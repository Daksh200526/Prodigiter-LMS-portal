import os
import hashlib
import hmac
import json
import tempfile
import unittest
import uuid
import time
import datetime
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

os.environ["DATABASE_URL"] = "sqlite:///test_lms.db"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import database
import main
import auth
import otp_security
import seed_db
from database import Base


class TestLMSAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        main.settings.DEV_MODE = False
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.testing_session_local = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=cls.engine,
        )

        database.engine = cls.engine
        database.SessionLocal = cls.testing_session_local
        Base.metadata.create_all(bind=cls.engine)

        def override_get_db():
            db = cls.testing_session_local()
            try:
                yield db
            finally:
                db.close()

        main.app.dependency_overrides[main.get_db] = override_get_db
        cls.client = TestClient(main.app)

    @classmethod
    def tearDownClass(cls):
        main.app.dependency_overrides.clear()

    def test_root_serves_index(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Prodigiter LMS Portal", response.text)

    def test_student_registration_and_login(self):
        email = "newstudent@example.com"
        phone = "+919900000001"
        password = "StrongPass123!"

        register_response = self.client.post(
            "/auth/register-student",
            json={
                "name": "New Student",
                "email": email,
                "phone_number": phone,
                "password": password,
                "course_subject": "Web Development",
            },
        )
        self.assertEqual(register_response.status_code, 200, register_response.text)
        token = register_response.json()["access_token"]
        self.assertTrue(token)

        duplicate_phone_response = self.client.post(
            "/auth/register-student",
            json={
                "name": "Duplicate Phone Student",
                "email": "duplicate-phone@example.com",
                "phone_number": "9900000001",
                "password": password,
                "course_subject": "Web Development",
            },
        )
        login_response = self.client.post(
            "/auth/login-password",
            json={"email": email, "password": password},
        )
        self.assertEqual(login_response.status_code, 200, login_response.text)
        self.assertEqual(login_response.json()["role"], "student")
        self.assertEqual(duplicate_phone_response.status_code, 400)

    def test_enrolled_students_receive_projects_in_a_repeating_sequence(self):
        db = self.testing_session_local()
        trainer_id = str(uuid.uuid4())
        course_id = str(uuid.uuid4())
        module_id = str(uuid.uuid4())
        students = []
        try:
            trainer = main.models.User(
                id=trainer_id,
                name="Project Trainer",
                email=f"{trainer_id}@example.com",
                role="trainer",
                specialization="Web Development",
            )
            course = main.models.Course(
                id=course_id,
                title="Project Rotation Course",
                trainer_id=trainer_id,
            )
            module = main.models.Module(
                id=module_id,
                course_id=course_id,
                order_index=1,
                title="Project Lesson",
                video_url="https://example.com/lesson",
                assignment_prompt="Complete your assigned project",
            )
            for student_index in range(12):
                student_id = str(uuid.uuid4())
                student = main.models.User(
                    id=student_id,
                    name=f"Project Student {student_index + 1}",
                    email=f"{student_id}@example.com",
                    role="student",
                    course_subject="Web Development",
                    is_access_granted=True,
                )
                students.append(student)

            db.add_all([trainer, course, module, *students])
            db.commit()
            for student in students:
                main._enroll_student_in_course(db, student, course)
            db.commit()

            enrollments = db.query(main.models.Enrollment).filter_by(
                course_id=course_id,
            ).all()
            project_indexes_by_student = {
                enrollment.student_id: enrollment.project_index
                for enrollment in enrollments
            }
            self.assertEqual(
                [project_indexes_by_student[student.id] for student in students],
                [
                    index % 10
                    for index in range(12)
                ],
            )

            token = auth.create_access_token({
                "sub": students[0].id,
                "role": "student",
            })
            dashboard_response = self.client.get(
                "/student/dashboard",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(dashboard_response.status_code, 200)
            self.assertEqual(
                dashboard_response.json()["courses"][0]["modules"][0]["assignment_link"],
                main.get_project_assignment_link(1, 0),
            )
            self.assertEqual(
                dashboard_response.json()["courses"][0]["modules"][0]["assignment_prompt"],
                "Complete your assigned project",
            )
            for lesson_order in range(1, 13):
                lesson_links = [
                    main.get_project_assignment_link(lesson_order, index)
                    for index in range(10)
                ]
                self.assertEqual(len(set(lesson_links)), 10)
            all_task_links = {
                main.get_project_assignment_link(lesson_order, project_index)
                for lesson_order in range(1, 13)
                for project_index in range(10)
            }
            self.assertEqual(len(all_task_links), 120)
            self.assertNotIn(
                "project_link",
                dashboard_response.json()["courses"][0]["modules"][0],
            )
        finally:
            db.close()

    def test_assignment_completion_starts_24_hour_unlock_timer(self):
        db = self.testing_session_local()
        try:
            trainer_id = str(uuid.uuid4())
            student_id = str(uuid.uuid4())
            course_id = str(uuid.uuid4())
            enrollment_id = str(uuid.uuid4())
            trainer = main.models.User(
                id=trainer_id,
                name="Trainer",
                email=f"{trainer_id}@example.com",
                role="trainer",
            )
            student = main.models.User(
                id=student_id,
                name="Student",
                email=f"{student_id}@example.com",
                role="student",
                is_access_granted=True,
            )
            course = main.models.Course(
                id=course_id,
                title="Unlock Timer Course",
                trainer_id=trainer_id,
            )
            modules = [
                main.models.Module(
                    id=str(uuid.uuid4()),
                    course_id=course_id,
                    order_index=order_index,
                    title=f"Lesson {order_index}",
                    video_url=f"https://example.com/lesson-{order_index}",
                    assignment_prompt="Complete the task.",
                )
                for order_index in range(1, 4)
            ]
            enrollment = main.models.Enrollment(
                id=enrollment_id,
                student_id=student_id,
                course_id=course_id,
                project_index=0,
            )
            intro_progress = main.models.StudentProgress(
                id=str(uuid.uuid4()),
                enrollment_id=enrollment_id,
                module_id=modules[0].id,
                is_video_completed=True,
                assignment_completed=True,
                assignment_completed_at=datetime.datetime.now(
                    datetime.timezone.utc
                ) - datetime.timedelta(hours=23),
            )
            task_progress = main.models.StudentProgress(
                id=str(uuid.uuid4()),
                enrollment_id=enrollment_id,
                module_id=modules[1].id,
                is_video_completed=True,
                assignment_completed=True,
                assignment_completed_at=datetime.datetime.now(
                    datetime.timezone.utc
                ) - datetime.timedelta(hours=23),
            )
            db.add_all([
                trainer, student, course, *modules, enrollment,
                intro_progress, task_progress,
            ])
            db.commit()
            progress_by_module = {
                intro_progress.module_id: intro_progress,
                task_progress.module_id: task_progress,
            }
            now = datetime.datetime.now(datetime.timezone.utc)

            lesson_two_unlocked = main._module_unlock_status(
                db, enrollment, modules, 1, now, progress_by_module
            )
            lesson_three_locked = main._module_unlock_status(
                db, enrollment, modules, 2, now, progress_by_module
            )
            task_progress.assignment_completed_at -= datetime.timedelta(hours=1)
            db.commit()
            lesson_three_unlocked = main._module_unlock_status(
                db, enrollment, modules, 2, now, progress_by_module
            )

            self.assertFalse(lesson_two_unlocked[0])
            self.assertIn("24h cooldown", lesson_two_unlocked[1])
            self.assertFalse(lesson_three_locked[0])
            self.assertIn("24h cooldown", lesson_three_locked[1])
            self.assertTrue(lesson_three_unlocked[0])
        finally:
            db.close()

    def test_task_review_qr_and_openai_review_flow(self):
        db = self.testing_session_local()
        try:
            trainer_id = str(uuid.uuid4())
            student_id = str(uuid.uuid4())
            course_id = str(uuid.uuid4())
            enrollment_id = str(uuid.uuid4())
            module_id = str(uuid.uuid4())
            progress_id = str(uuid.uuid4())
            trainer = main.models.User(
                id=trainer_id,
                name="Review Trainer",
                email=f"{trainer_id}@example.com",
                role="trainer",
            )
            student = main.models.User(
                id=student_id,
                name="Review Student",
                email=f"{student_id}@example.com",
                role="student",
                is_access_granted=True,
            )
            course = main.models.Course(
                id=course_id,
                title="Review Course",
                trainer_id=trainer_id,
            )
            module = main.models.Module(
                id=module_id,
                course_id=course_id,
                order_index=1,
                title="Task Lesson",
                video_url="https://example.com/task-video",
                assignment_prompt="Complete this task.",
            )
            enrollment = main.models.Enrollment(
                id=enrollment_id,
                student_id=student_id,
                course_id=course_id,
                project_index=0,
            )
            progress = main.models.StudentProgress(
                id=progress_id,
                enrollment_id=enrollment_id,
                module_id=module_id,
                is_video_completed=True,
                assignment_completed=True,
                assignment_completed_at=datetime.datetime.now(datetime.timezone.utc),
            )
            db.add_all([trainer, student, course, module, enrollment, progress])
            db.commit()
            student_headers = {
                "Authorization": f"Bearer {auth.create_access_token({'sub': student_id, 'role': 'student'})}",
            }

            with patch.object(main.settings, "PUBLIC_BASE_URL", ""):
                qr_response = self.client.get(
                    f"/student/enrollments/{enrollment_id}/modules/{module_id}/review-qr",
                    headers=student_headers,
                )
            self.assertEqual(qr_response.status_code, 200, qr_response.text)
            self.assertTrue(qr_response.json()["qr_code"])
            review_url = urlparse(qr_response.json()["review_url"])
            route_parts = review_url.path.strip("/").split("/")
            self.assertEqual(route_parts[:2], ["task-review", progress_id])
            token = route_parts[2]

            page_response = self.client.get(review_url.path)
            details_response = self.client.get(
                f"/api/task-review/{progress_id}/{token}",
            )
            self.assertEqual(page_response.status_code, 200)
            self.assertIn("Task Reflection", page_response.text)
            self.assertEqual(details_response.status_code, 200)
            self.assertEqual(details_response.json()["lesson_title"], "Task Lesson")
            answers = {
                question["id"]: question["options"][0]["value"]
                for question in details_response.json()["questions"]
            }
            invalid_answers = self.client.post(
                f"/api/task-review/{progress_id}/{token}/generate",
                json={"answers": {}},
            )

            mock_response = MagicMock()
            mock_response.__enter__.return_value = mock_response
            mock_response.read.return_value = json.dumps({
                "choices": [{
                    "message": {
                        "content": "The task was useful and helped me practice problem solving.",
                    },
                }],
            }).encode("utf-8")
            with patch.object(main.settings, "OPENAI_API_KEY", "test-api-key"), \
                    patch.object(main.settings, "OPENAI_MODEL", "test-model"), \
                    patch.object(
                        main.settings,
                        "REVIEW_REDIRECT_URL",
                        "https://example.com/reviews",
                    ), \
                    patch("review_service.urlopen", return_value=mock_response) as openai_request:
                generated_response = self.client.post(
                    f"/api/task-review/{progress_id}/{token}/generate",
                    json={"answers": answers},
                )

            self.assertEqual(invalid_answers.status_code, 422)
            self.assertEqual(generated_response.status_code, 200, generated_response.text)
            self.assertEqual(
                generated_response.json()["review"],
                "The task was useful and helped me practice problem solving.",
            )
            self.assertEqual(
                generated_response.json()["redirect_url"],
                "https://example.com/reviews",
            )
            self.assertNotIn(student.email, openai_request.call_args.args[0].data.decode())
            db.refresh(progress)
            self.assertIsNotNone(progress.review_generated_at)
            self.assertEqual(
                progress.generated_review,
                generated_response.json()["review"],
            )
        finally:
            db.close()

    def test_course_subject_registration_filters_student_and_trainer_content(self):
        db = self.testing_session_local()
        web_trainer_id = str(uuid.uuid4())
        python_trainer_id = str(uuid.uuid4())
        web_student_id = str(uuid.uuid4())
        python_student_id = str(uuid.uuid4())
        web_course_id = str(uuid.uuid4())
        python_course_id = str(uuid.uuid4())
        try:
            web_trainer = main.models.User(
                id=web_trainer_id,
                name="Web Trainer",
                email=f"{web_trainer_id}@example.com",
                role="trainer",
                specialization="Web Development",
                is_access_granted=True,
            )
            python_trainer = main.models.User(
                id=python_trainer_id,
                name="Python Trainer",
                email=f"{python_trainer_id}@example.com",
                role="trainer",
                specialization="Python Programming",
                is_access_granted=True,
            )
            db.add_all([web_trainer, python_trainer])
            db.commit()

            subjects_response = self.client.get("/courses/subjects")
            self.assertEqual(subjects_response.status_code, 200)
            self.assertEqual(subjects_response.json()["subjects"], list(main.COURSE_SUBJECTS))

            registration_data = {
                "name": "Course Student",
                "email": f"{uuid.uuid4()}@example.com",
                "phone_number": f"9{uuid.uuid4().int % 1000000000:09d}",
                "password": "StrongPass123!",
            }
            invalid_registration = self.client.post(
                "/auth/register-student",
                json={**registration_data, "course_subject": "Unlisted Subject"},
            )
            web_registration = self.client.post(
                "/auth/register-student",
                json={
                    **registration_data,
                    "course_subject": "Web Development",
                },
            )
            self.assertEqual(invalid_registration.status_code, 422)
            self.assertEqual(web_registration.status_code, 200)
            web_student_id = web_registration.json()["user_id"]

            web_headers = {
                "Authorization": f"Bearer {auth.create_access_token({'sub': web_trainer_id, 'role': 'trainer'})}"
            }
            python_headers = {
                "Authorization": f"Bearer {auth.create_access_token({'sub': python_trainer_id, 'role': 'trainer'})}"
            }
            course_payload = {
                "title": "Subject Course",
                "description": "Course for one subject",
                "modules": [{
                    "order_index": 1,
                    "title": "Lesson One",
                    "video_url": "https://example.com/lesson",
                    "assignment_prompt": "Complete the lesson",
                }],
            }
            web_course_response = self.client.post(
                "/trainer/courses",
                json=course_payload,
                headers=web_headers,
            )
            python_course_response = self.client.post(
                "/trainer/courses",
                json={**course_payload, "title": "Python Subject Course"},
                headers=python_headers,
            )
            self.assertEqual(web_course_response.status_code, 201)
            self.assertEqual(python_course_response.status_code, 201)

            python_registration = self.client.post(
                "/auth/register-student",
                json={
                    **registration_data,
                    "name": "Python Student",
                    "email": f"{uuid.uuid4()}@example.com",
                    "phone_number": f"9{uuid.uuid4().int % 1000000000:09d}",
                    "course_subject": "Python Programming",
                },
            )
            self.assertEqual(python_registration.status_code, 200)
            python_student_id = python_registration.json()["user_id"]

            web_student = db.query(main.models.User).filter_by(id=web_student_id).one()
            python_student = db.query(main.models.User).filter_by(id=python_student_id).one()
            web_student.is_access_granted = True
            python_student.is_access_granted = True
            web_course_id = web_course_response.json()["course_id"]
            python_course_id = python_course_response.json()["course_id"]
            python_course = db.query(main.models.Course).filter_by(
                id=python_course_id,
            ).one()
            web_enrollment = db.query(main.models.Enrollment).filter_by(
                student_id=web_student_id,
                course_id=web_course_id,
            ).one()
            python_enrollment = db.query(main.models.Enrollment).filter_by(
                student_id=python_student_id,
                course_id=python_course_id,
            ).one()
            foreign_enrollment = main.models.Enrollment(
                id=str(uuid.uuid4()),
                student_id=web_student_id,
                course_id=python_course_id,
            )
            db.add(foreign_enrollment)
            db.commit()

            web_dashboard = self.client.get(
                "/student/dashboard",
                headers={"Authorization": f"Bearer {web_registration.json()['access_token']}"},
            )
            python_dashboard = self.client.get(
                "/student/dashboard",
                headers={"Authorization": f"Bearer {python_registration.json()['access_token']}"},
            )
            web_trainer_dashboard = self.client.get("/trainer/dashboard", headers=web_headers)
            python_trainer_dashboard = self.client.get(
                "/trainer/dashboard",
                headers=python_headers,
            )
            mismatched_completion = self.client.post(
                f"/student/complete-video/{foreign_enrollment.id}/{python_course.modules[0].id}",
                headers={"Authorization": f"Bearer {web_registration.json()['access_token']}"},
            )

            self.assertEqual(web_dashboard.status_code, 200)
            self.assertEqual(python_dashboard.status_code, 200)
            web_course_titles = [
                course["course_title"] for course in web_dashboard.json()["courses"]
            ]
            python_course_titles = [
                course["course_title"] for course in python_dashboard.json()["courses"]
            ]
            self.assertIn("Subject Course", web_course_titles)
            self.assertNotIn("Python Subject Course", web_course_titles)
            self.assertEqual(python_course_titles, ["Python Subject Course"])
            web_trainer_student_ids = [
                student["student_id"]
                for course in web_trainer_dashboard.json()["courses"]
                for student in course["students"]
            ]
            self.assertIn(web_student_id, web_trainer_student_ids)
            self.assertNotIn(python_student_id, web_trainer_student_ids)
            self.assertEqual(
                [student["student_id"] for course in python_trainer_dashboard.json()["courses"]
                 for student in course["students"]],
                [python_student_id],
            )
            self.assertEqual(mismatched_completion.status_code, 403)
            self.assertIsNotNone(web_enrollment)
            self.assertIsNotNone(python_enrollment)
        finally:
            db.close()

    def test_invalid_credentials_are_rejected(self):
        response = self.client.post(
            "/auth/login-password",
            json={"email": "missing@example.com", "password": "wrongpass"},
        )
        self.assertEqual(response.status_code, 401)

    def test_profile_route_requires_auth(self):
        response = self.client.get("/user/profile-details")
        self.assertEqual(response.status_code, 401)

    def test_admin_and_trainer_profiles_include_specialization_and_role(self):
        db = self.testing_session_local()
        admin_id = str(uuid.uuid4())
        trainer_id = str(uuid.uuid4())
        try:
            admin = main.models.User(
                id=admin_id,
                name="System Admin",
                email=f"{admin_id}@example.com",
                role="admin",
                specialization="Platform administration",
            )
            trainer = main.models.User(
                id=trainer_id,
                name="Course Trainer",
                email=f"{trainer_id}@example.com",
                role="trainer",
                specialization="Web development",
            )
            db.add_all([admin, trainer])
            db.commit()

            profiles = {}
            for user_id, role in ((admin_id, "admin"), (trainer_id, "trainer")):
                token = auth.create_access_token({"sub": user_id, "role": role})
                response = self.client.get(
                    "/user/profile-details",
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(response.status_code, 200)
                profiles[role] = response.json()

            self.assertEqual(profiles["admin"]["name"], "System Admin")
            self.assertEqual(profiles["admin"]["email"], f"{admin_id}@example.com")
            self.assertEqual(profiles["admin"]["specialization"], "Platform administration")
            self.assertEqual(profiles["admin"]["role"], "admin")
            self.assertEqual(profiles["trainer"]["name"], "Course Trainer")
            self.assertEqual(profiles["trainer"]["email"], f"{trainer_id}@example.com")
            self.assertEqual(profiles["trainer"]["specialization"], "Web development")
            self.assertEqual(profiles["trainer"]["role"], "trainer")
        finally:
            db.close()

    def test_admin_can_create_trainer_account_that_can_log_in(self):
        db = self.testing_session_local()
        admin_id = str(uuid.uuid4())
        try:
            admin = main.models.User(
                id=admin_id,
                name="Administrator",
                email=f"{admin_id}@example.com",
                role="admin",
            )
            db.add(admin)
            db.commit()
            admin_token = auth.create_access_token({"sub": admin_id, "role": "admin"})
            headers = {"Authorization": f"Bearer {admin_token}"}
            payload = {
                "name": "New Trainer",
                "email": f"trainer-{uuid.uuid4()}@example.com",
                "phone_number": f"9{uuid.uuid4().int % 1000000000:09d}",
                "specialization": "Data science",
                "password": "TrainerLogin123!",
            }

            create_response = self.client.post(
                "/admin/trainers",
                json=payload,
                headers=headers,
            )
            trainer_id = create_response.json()["trainer_id"]
            trainer = db.query(main.models.User).filter_by(id=trainer_id).one()
            login_response = self.client.post(
                "/auth/login-password",
                json={"email": payload["email"].upper(), "password": payload["password"]},
            )
            duplicate_response = self.client.post(
                "/admin/trainers",
                json={**payload, "email": payload["email"].upper()},
                headers=headers,
            )
            student_id = str(uuid.uuid4())
            student_phone = f"9{uuid.uuid4().int % 1000000000:09d}"
            student = main.models.User(
                id=student_id,
                name="Student",
                email=f"{student_id}@example.com",
                phone_number=student_phone,
                role="student",
            )
            db.add(student)
            db.commit()
            duplicate_student_phone_response = self.client.post(
                "/admin/trainers",
                json={
                    **payload,
                    "email": f"duplicate-phone-{uuid.uuid4()}@example.com",
                    "phone_number": student_phone,
                },
                headers=headers,
            )
            student_token = auth.create_access_token({"sub": student_id, "role": "student"})
            forbidden_response = self.client.post(
                "/admin/trainers",
                json={
                    **payload,
                    "email": f"forbidden-{uuid.uuid4()}@example.com",
                },
                headers={"Authorization": f"Bearer {student_token}"},
            )

            self.assertEqual(create_response.status_code, 201, create_response.text)
            self.assertEqual(trainer.name, payload["name"])
            self.assertEqual(trainer.email, payload["email"])
            self.assertEqual(trainer.phone_number, payload["phone_number"])
            self.assertEqual(trainer.specialization, "Data Science")
            self.assertTrue(trainer.is_access_granted)
            self.assertTrue(auth.verify_password(payload["password"], trainer.hashed_password))
            self.assertEqual(login_response.status_code, 200, login_response.text)
            self.assertEqual(login_response.json()["role"], "trainer")
            self.assertEqual(duplicate_response.status_code, 409)
            self.assertEqual(duplicate_student_phone_response.status_code, 409)
            self.assertEqual(forbidden_response.status_code, 403)
        finally:
            db.close()

    def test_trainer_deletion_request_requires_admin_approval(self):
        db = self.testing_session_local()
        trainer_id = str(uuid.uuid4())
        other_trainer_id = str(uuid.uuid4())
        admin_id = str(uuid.uuid4())
        student_id = str(uuid.uuid4())
        outsider_id = str(uuid.uuid4())
        course_id = str(uuid.uuid4())
        enrollment_id = str(uuid.uuid4())
        try:
            trainer = main.models.User(
                id=trainer_id,
                name="Trainer",
                email=f"{trainer_id}@example.com",
                role="trainer",
            )
            other_trainer = main.models.User(
                id=other_trainer_id,
                name="Other Trainer",
                email=f"{other_trainer_id}@example.com",
                role="trainer",
            )
            admin = main.models.User(
                id=admin_id,
                name="Administrator",
                email=f"{admin_id}@example.com",
                role="admin",
            )
            student = main.models.User(
                id=student_id,
                name="Enrolled Student",
                email=f"{student_id}@example.com",
                role="student",
            )
            outsider = main.models.User(
                id=outsider_id,
                name="Not Enrolled",
                email=f"{outsider_id}@example.com",
                role="student",
            )
            course = main.models.Course(
                id=course_id,
                title="Trainer Course",
                trainer_id=trainer_id,
            )
            enrollment = main.models.Enrollment(
                id=enrollment_id,
                student_id=student_id,
                course_id=course_id,
            )
            db.add_all([trainer, other_trainer, admin, student, outsider, course, enrollment])
            db.commit()

            trainer_token = auth.create_access_token({"sub": trainer_id, "role": "trainer"})
            trainer_headers = {"Authorization": f"Bearer {trainer_token}"}
            admin_token = auth.create_access_token({"sub": admin_id, "role": "admin"})
            admin_headers = {"Authorization": f"Bearer {admin_token}"}
            request_payload = {
                "student_id": student_id,
                "reason": "Repeatedly violates the course participation policy.",
            }
            empty_reason_response = self.client.post(
                "/trainer/request-student-deletion",
                json={**request_payload, "reason": "   "},
                headers=trainer_headers,
            )
            outsider_response = self.client.post(
                "/trainer/request-student-deletion",
                json={**request_payload, "student_id": outsider_id},
                headers=trainer_headers,
            )
            other_trainer_token = auth.create_access_token({
                "sub": other_trainer_id,
                "role": "trainer",
            })
            other_trainer_response = self.client.post(
                "/trainer/request-student-deletion",
                json=request_payload,
                headers={"Authorization": f"Bearer {other_trainer_token}"},
            )
            request_response = self.client.post(
                "/trainer/request-student-deletion",
                json=request_payload,
                headers=trainer_headers,
            )
            duplicate_response = self.client.post(
                "/trainer/request-student-deletion",
                json=request_payload,
                headers=trainer_headers,
            )
            trainer_requests_response = self.client.get(
                "/trainer/deletion-requests",
                headers=trainer_headers,
            )
            pending_response = self.client.get(
                "/admin/pending-deletion-list",
                headers=admin_headers,
            )
            deletion_request = pending_response.json()[0]
            approval_response = self.client.post(
                "/admin/process-deletion",
                json={
                    "request_id": request_response.json()["request_id"],
                    "approved": True,
                },
                headers=admin_headers,
            )
            repeated_approval_response = self.client.post(
                "/admin/process-deletion",
                json={
                    "request_id": request_response.json()["request_id"],
                    "approved": True,
                },
                headers=admin_headers,
            )
            student_after_approval = db.query(main.models.User).filter_by(
                id=student_id
            ).first()
            unauthorized_final_deletion = self.client.post(
                f"/trainer/delete-approved-student/{student_id}",
                headers={"Authorization": f"Bearer {other_trainer_token}"},
            )
            final_deletion_response = self.client.post(
                f"/trainer/delete-approved-student/{student_id}",
                headers=trainer_headers,
            )

            self.assertEqual(empty_reason_response.status_code, 422)
            self.assertEqual(outsider_response.status_code, 404)
            self.assertEqual(other_trainer_response.status_code, 404)
            self.assertEqual(request_response.status_code, 201)
            self.assertEqual(duplicate_response.status_code, 409)
            self.assertEqual(trainer_requests_response.status_code, 200)
            self.assertEqual(trainer_requests_response.json()[0]["student_id"], student_id)
            self.assertEqual(pending_response.status_code, 200)
            self.assertEqual(deletion_request["trainer_email"], trainer.email)
            self.assertEqual(deletion_request["student_email"], f"{student_id}@example.com")
            self.assertEqual(deletion_request["reason"], request_payload["reason"])
            self.assertEqual(approval_response.status_code, 200, approval_response.text)
            self.assertIn("trainer can now delete", approval_response.json()["message"])
            self.assertIsNotNone(student_after_approval)
            self.assertEqual(repeated_approval_response.status_code, 409)
            self.assertEqual(unauthorized_final_deletion.status_code, 403)
            self.assertEqual(final_deletion_response.status_code, 200)
            self.assertIsNone(db.query(main.models.User).filter_by(id=student_id).first())

            rejection_student_id = str(uuid.uuid4())
            rejection_student = main.models.User(
                id=rejection_student_id,
                name="Student Kept",
                email=f"{rejection_student_id}@example.com",
                role="student",
            )
            rejection_enrollment = main.models.Enrollment(
                id=str(uuid.uuid4()),
                student_id=rejection_student_id,
                course_id=course_id,
            )
            db.add_all([rejection_student, rejection_enrollment])
            db.commit()
            rejected_request_response = self.client.post(
                "/trainer/request-student-deletion",
                json={
                    "student_id": rejection_student_id,
                    "reason": "Request should be rejected.",
                },
                headers=trainer_headers,
            )
            rejection_response = self.client.post(
                "/admin/process-deletion",
                json={
                    "request_id": rejected_request_response.json()["request_id"],
                    "approved": False,
                },
                headers=admin_headers,
            )
            self.assertEqual(rejection_response.status_code, 200)
            self.assertIsNotNone(
                db.query(main.models.User).filter_by(id=rejection_student_id).first()
            )
        finally:
            db.close()

    def test_student_cannot_complete_or_submit_locked_lesson(self):
        db = self.testing_session_local()
        try:
            trainer_id = str(uuid.uuid4())
            student_id = str(uuid.uuid4())
            course_id = str(uuid.uuid4())
            enrollment_id = str(uuid.uuid4())
            first_module_id = str(uuid.uuid4())
            locked_module_id = str(uuid.uuid4())

            trainer = main.models.User(
                id=trainer_id,
                name="Trainer",
                email=f"{trainer_id}@example.com",
                role="trainer",
            )
            student = main.models.User(
                id=student_id,
                name="Student",
                email=f"{student_id}@example.com",
                role="student",
                is_access_granted=True,
            )
            course = main.models.Course(
                id=course_id,
                title="Course",
                trainer_id=trainer_id,
            )
            enrollment = main.models.Enrollment(
                id=enrollment_id,
                student_id=student_id,
                course_id=course_id,
            )
            first_module = main.models.Module(
                id=first_module_id,
                course_id=course_id,
                order_index=1,
                title="First lesson",
                video_url="https://example.com/first.mp4",
                assignment_prompt="Complete the task.",
                assignment_link="https://example.com/first-assignment",
            )
            locked_module = main.models.Module(
                id=locked_module_id,
                course_id=course_id,
                order_index=3,
                title="Locked lesson",
                video_url="https://example.com/locked.mp4",
                assignment_prompt="Complete the task.",
                assignment_link="https://example.com/locked-assignment",
            )
            task_module = main.models.Module(
                id=str(uuid.uuid4()),
                course_id=course_id,
                order_index=2,
                title="Task lesson",
                video_url="https://example.com/task.mp4",
                assignment_prompt="Complete the task.",
            )
            db.add_all([
                trainer, student, course, enrollment, first_module,
                task_module, locked_module,
            ])
            db.commit()

            token = main.auth.create_access_token({"sub": student_id, "role": "student"})
            headers = {"Authorization": f"Bearer {token}"}
            dashboard_response = self.client.get("/student/dashboard", headers=headers)
            dashboard_modules = dashboard_response.json()["courses"][0]["modules"]
            self.assertEqual(
                dashboard_response.json()["courses"][0]["trainer_name"],
                "Trainer",
            )
            self.assertFalse(dashboard_modules[0]["assignment_completed"])
            incomplete_assignment_response = self.client.post(
                "/student/submit-assignment",
                data={
                    "enrollment_id": enrollment_id,
                    "module_id": first_module_id,
                    "submission_text": "Done",
                },
                headers=headers,
            )
            available_video_response = self.client.post(
                f"/student/complete-video/{enrollment_id}/{first_module_id}",
                headers=headers,
            )
            assignment_submission_response = self.client.post(
                "/student/submit-assignment",
                data={
                    "enrollment_id": enrollment_id,
                    "module_id": first_module_id,
                    "submission_text": "Done",
                },
                headers=headers,
            )
            video_response = self.client.post(
                f"/student/complete-video/{enrollment_id}/{locked_module_id}",
                headers=headers,
            )
            assignment_response = self.client.post(
                "/student/submit-assignment",
                data={
                    "enrollment_id": enrollment_id,
                    "module_id": locked_module_id,
                    "submission_text": "Done",
                },
                headers=headers,
            )

            self.assertEqual(dashboard_response.status_code, 200)
            self.assertEqual(dashboard_modules[0]["video_url"], "https://example.com/first.mp4")
            self.assertIsNone(dashboard_modules[1]["video_url"])
            self.assertIsNone(dashboard_modules[1]["assignment_link"])
            self.assertEqual(incomplete_assignment_response.status_code, 400)
            self.assertEqual(available_video_response.status_code, 200)
            self.assertEqual(assignment_submission_response.status_code, 200)
            self.assertEqual(video_response.status_code, 403)
            self.assertEqual(assignment_response.status_code, 403)
        finally:
            db.close()

    def test_assignment_submission_and_qr_trigger_require_trainer_link_open(self):
        db = self.testing_session_local()
        trainer_id = str(uuid.uuid4())
        student_id = str(uuid.uuid4())
        course_id = str(uuid.uuid4())
        enrollment_id = str(uuid.uuid4())
        module_id = str(uuid.uuid4())
        try:
            trainer = main.models.User(
                id=trainer_id,
                name="Trainer",
                email=f"{trainer_id}@example.com",
                role="trainer",
            )
            student = main.models.User(
                id=student_id,
                name="Student",
                email=f"{student_id}@example.com",
                role="student",
                is_access_granted=True,
            )
            course = main.models.Course(
                id=course_id,
                title="Course",
                trainer_id=trainer_id,
            )
            enrollment = main.models.Enrollment(
                id=enrollment_id,
                student_id=student_id,
                course_id=course_id,
            )
            module = main.models.Module(
                id=module_id,
                course_id=course_id,
                order_index=2,
                title="Lesson",
                video_url="https://example.com/video.mp4",
                assignment_prompt="Complete the task.",
                assignment_link="https://example.com/assignment",
            )
            progress = main.models.StudentProgress(
                id=str(uuid.uuid4()),
                enrollment_id=enrollment_id,
                module_id=module_id,
                is_video_completed=True,
                assignment_status="not_started",
            )
            db.add_all([trainer, student, course, enrollment, module, progress])
            db.commit()

            student_token = auth.create_access_token({"sub": student_id, "role": "student"})
            student_headers = {"Authorization": f"Bearer {student_token}"}
            submit_payload = {
                "enrollment_id": enrollment_id,
                "module_id": module_id,
                "submission_text": "https://example.com/my-work",
            }
            blank_submission_response = self.client.post(
                "/student/submit-assignment",
                data={**submit_payload, "submission_text": "   "},
                headers=student_headers,
            )

            timestamp = str(int(time.time()))
            body = json.dumps({
                "enrollment_id": enrollment_id,
                "module_id": module_id,
                "completed": True,
            }).encode()
            secret = "test-assignment-webhook-secret-long-enough"
            signature = hmac.new(
                secret.encode(),
                timestamp.encode() + b"." + body,
                hashlib.sha256,
            ).hexdigest()
            callback_headers = {
                "X-Assignment-Timestamp": timestamp,
                "X-Assignment-Signature": signature,
                "Content-Type": "application/json",
            }
            with patch.object(main.settings, "ASSIGNMENT_WEBHOOK_SECRET", secret):
                invalid_signature_response = self.client.post(
                    "/integrations/assignment-completed",
                    content=body,
                    headers={**callback_headers, "X-Assignment-Signature": "0" * 64},
                )
                submit_response = self.client.post(
                    "/student/submit-assignment",
                    data=submit_payload,
                    headers=student_headers,
                )
                waiting_dashboard = self.client.get(
                    "/student/dashboard",
                    headers=student_headers,
                )

            self.assertEqual(invalid_signature_response.status_code, 401)
            self.assertEqual(submit_response.status_code, 200)
            self.assertEqual(blank_submission_response.status_code, 422)
            waiting_module = waiting_dashboard.json()["courses"][0]["modules"][0]
            self.assertFalse(waiting_module["assignment_completed"])
            self.assertFalse(waiting_module["review_available"])

            trainer_token = auth.create_access_token({"sub": trainer_id, "role": "trainer"})
            trainer_headers = {"Authorization": f"Bearer {trainer_token}"}
            progress = db.query(main.models.StudentProgress).filter_by(
                enrollment_id=enrollment_id,
                module_id=module_id,
            ).one()
            pending_response = self.client.get(
                "/trainer/pending-assignments",
                headers=trainer_headers,
            )
            pending_assignment = pending_response.json()[0]
            self.assertEqual(pending_assignment["submission_notes"], "https://example.com/my-work")
            self.assertEqual(
                pending_assignment["assignment_link"],
                main.get_project_assignment_link(2, None)
                or "https://example.com/assignment",
            )
            self.assertEqual(pending_assignment["student_email"], student.email)
            student_open_submission_response = self.client.post(
                f"/trainer/assignments/{progress.id}/open-submission",
                headers=student_headers,
            )
            open_submission_response = self.client.post(
                f"/trainer/assignments/{progress.id}/open-submission",
                headers=trainer_headers,
            )
            after_open_dashboard = self.client.get(
                "/student/dashboard",
                headers=student_headers,
            )
            qr_response = self.client.get(
                f"/student/enrollments/{enrollment_id}/modules/{module_id}/review-qr",
                headers=student_headers,
            )
            self.assertEqual(student_open_submission_response.status_code, 403)
            self.assertEqual(open_submission_response.status_code, 200)
            self.assertTrue(
                after_open_dashboard.json()["courses"][0]["modules"][0]["review_available"]
            )
            self.assertEqual(qr_response.status_code, 200)
            self.assertTrue(qr_response.json()["qr_code"])
            db.refresh(progress)
            self.assertTrue(progress.assignment_completed)
            self.assertIsNotNone(progress.assignment_completed_at)

            rejected_response = self.client.post(
                "/trainer/evaluate-assignment",
                json={
                    "progress_id": progress.id,
                    "is_approved": False,
                    "feedback": "Please revise your work.",
                },
                headers=trainer_headers,
            )
            updated_submission_response = self.client.post(
                "/student/submit-assignment",
                data={
                    **submit_payload,
                    "submission_text": "Updated work: https://example.com/my-work",
                },
                headers=student_headers,
            )
            db.refresh(progress)
            self.assertEqual(pending_response.status_code, 200)
            self.assertEqual(rejected_response.status_code, 200)
            self.assertEqual(updated_submission_response.status_code, 200)
            self.assertEqual(
                progress.submission_notes,
                "Updated work: https://example.com/my-work",
            )
            self.assertEqual(progress.assignment_status, "submitted")
            self.assertFalse(progress.assignment_completed)
            reopened_submission_response = self.client.post(
                f"/trainer/assignments/{progress.id}/open-submission",
                headers=trainer_headers,
            )
            db.refresh(progress)
            self.assertEqual(reopened_submission_response.status_code, 200)
            self.assertTrue(progress.assignment_completed)
            progress.assignment_completed = False
            progress.assignment_completed_at = None
            db.commit()
            approval_response = self.client.post(
                "/trainer/evaluate-assignment",
                json={
                    "progress_id": progress.id,
                    "is_approved": True,
                    "feedback": "Approved.",
                },
                headers=trainer_headers,
            )
            approved_dashboard = self.client.get(
                "/student/dashboard",
                headers=student_headers,
            )
            approved_qr_response = self.client.get(
                f"/student/enrollments/{enrollment_id}/modules/{module_id}/review-qr",
                headers=student_headers,
            )
            db.refresh(progress)
            self.assertEqual(approval_response.status_code, 200)
            self.assertTrue(progress.assignment_completed)
            self.assertIsNotNone(progress.assignment_completed_at)
            self.assertTrue(
                approved_dashboard.json()["courses"][0]["modules"][0]["review_available"]
            )
            self.assertEqual(approved_qr_response.status_code, 200)
            self.assertTrue(approved_qr_response.json()["qr_code"])
            progress.assignment_completed = False
            progress.assignment_completed_at = None
            db.commit()
            legacy_approved_dashboard = self.client.get(
                "/student/dashboard",
                headers=student_headers,
            )
            legacy_approved_qr = self.client.get(
                f"/student/enrollments/{enrollment_id}/modules/{module_id}/review-qr",
                headers=student_headers,
            )
            db.refresh(progress)
            self.assertTrue(
                legacy_approved_dashboard.json()["courses"][0]["modules"][0]["review_available"]
            )
            self.assertEqual(legacy_approved_qr.status_code, 200)
            self.assertTrue(legacy_approved_qr.json()["qr_code"])
            self.assertTrue(progress.assignment_completed)
            self.assertIsNotNone(progress.assignment_completed_at)
        finally:
            db.close()

    def test_trainer_can_create_course_with_lessons(self):
        db = self.testing_session_local()
        trainer_id = str(uuid.uuid4())
        student_id = str(uuid.uuid4())
        try:
            trainer = main.models.User(
                id=trainer_id,
                name="Course Trainer",
                email=f"{trainer_id}@example.com",
                role="trainer",
                specialization="Web Development",
            )
            student = main.models.User(
                id=student_id,
                name="Course Student",
                email=f"{student_id}@example.com",
                role="student",
                course_subject="Web Development",
            )
            db.add_all([trainer, student])
            db.commit()

            token = auth.create_access_token({"sub": trainer_id, "role": "trainer"})
            headers = {"Authorization": f"Bearer {token}"}
            payload = {
                "title": "API Basics",
                "description": "A course description",
                "modules": [
                    {
                        "order_index": 2,
                        "title": "Second lesson",
                        "video_url": "https://example.com/lesson-2.mp4",
                        "assignment_prompt": "Complete the next exercise",
                        "assignment_link": None,
                    },
                    {
                        "order_index": 1,
                        "title": "First lesson",
                        "video_url": "https://example.com/lesson-1.mp4",
                        "assignment_prompt": "Complete the API exercise",
                        "assignment_link": "https://example.com/exercise",
                    },
                ],
            }
            response = self.client.post(
                "/trainer/courses",
                json=payload,
                headers=headers,
            )
            course = db.query(main.models.Course).filter_by(
                id=response.json()["course_id"]
            ).one()
            enrollment = db.query(main.models.Enrollment).filter_by(
                student_id=student_id,
                course_id=course.id,
            ).one()
            approved_progress = main.models.StudentProgress(
                id=str(uuid.uuid4()),
                enrollment_id=enrollment.id,
                module_id=course.modules[0].id,
                is_video_completed=True,
                assignment_completed=True,
                assignment_status="approved",
                is_approved=True,
            )
            db.add_all([enrollment, approved_progress])
            db.commit()
            dashboard_response = self.client.get(
                "/trainer/dashboard",
                headers=headers,
            )
            student_token = auth.create_access_token({"sub": student_id, "role": "student"})
            forbidden_response = self.client.post(
                "/trainer/courses",
                json=payload,
                headers={"Authorization": f"Bearer {student_token}"},
            )
            duplicate_order_response = self.client.post(
                "/trainer/courses",
                json={
                    **payload,
                    "modules": [payload["modules"][0], {
                        **payload["modules"][1],
                        "order_index": payload["modules"][0]["order_index"],
                    }],
                },
                headers=headers,
            )
            added_lesson_response = self.client.post(
                f"/trainer/courses/{course.id}/modules",
                json={
                    "order_index": 3,
                    "title": "Third lesson",
                    "video_url": "https://example.com/lesson-3.mp4",
                    "assignment_prompt": "Complete the final exercise",
                    "assignment_link": "https://example.com/final-exercise",
                },
                headers=headers,
            )
            invalid_order_response = self.client.post(
                f"/trainer/courses/{course.id}/modules",
                json={
                    "order_index": 3,
                    "title": "Wrong order",
                    "video_url": "https://example.com/wrong-order.mp4",
                    "assignment_prompt": "This should be rejected",
                },
                headers=headers,
            )
            other_trainer_id = str(uuid.uuid4())
            other_trainer = main.models.User(
                id=other_trainer_id,
                name="Other Trainer",
                email=f"{other_trainer_id}@example.com",
                role="trainer",
            )
            db.add(other_trainer)
            db.commit()
            other_trainer_token = auth.create_access_token({
                "sub": other_trainer_id,
                "role": "trainer",
            })
            unauthorized_course_response = self.client.post(
                f"/trainer/courses/{course.id}/modules",
                json={
                    "order_index": 3,
                    "title": "Unauthorized lesson",
                    "video_url": "https://example.com/unauthorized.mp4",
                    "assignment_prompt": "This should not be added",
                },
                headers={"Authorization": f"Bearer {other_trainer_token}"},
            )

            self.assertEqual(response.status_code, 201, response.text)
            course = db.query(main.models.Course).filter_by(
                id=response.json()["course_id"]
            ).one()
            self.assertEqual(course.trainer_id, trainer_id)
            self.assertEqual(
                [module.order_index for module in course.modules],
                [1, 2, 3],
            )
            self.assertEqual(
                course.modules[0].assignment_link,
                "https://example.com/exercise",
            )
            self.assertEqual(dashboard_response.status_code, 200)
            dashboard_course = next(
                item for item in dashboard_response.json()["courses"]
                if item["id"] == course.id
            )
            self.assertGreaterEqual(dashboard_course["total_enrolled"], 1)
            self.assertEqual(len(dashboard_course["modules"]), 2)
            course_student = next(
                student for student in dashboard_course["students"]
                if student["student_id"] == student_id
            )
            self.assertEqual(course_student["progress_percent"], 50)
            self.assertEqual(course_student["completed_slots"], 1)
            self.assertEqual(forbidden_response.status_code, 403)
            self.assertEqual(duplicate_order_response.status_code, 422)
            self.assertEqual(added_lesson_response.status_code, 201)
            self.assertEqual(added_lesson_response.json()["order_index"], 3)
            self.assertEqual(invalid_order_response.status_code, 422)
            self.assertEqual(unauthorized_course_response.status_code, 404)
            self.assertEqual(
                db.query(main.models.Module).filter_by(course_id=course.id).count(),
                3,
            )
        finally:
            db.close()

    def test_admin_and_trainer_student_lists_include_enrollment_progress(self):
        db = self.testing_session_local()
        trainer_id = str(uuid.uuid4())
        admin_id = str(uuid.uuid4())
        student_id = str(uuid.uuid4())
        course_id = str(uuid.uuid4())
        enrollment_id = str(uuid.uuid4())
        try:
            trainer = main.models.User(
                id=trainer_id,
                name="Progress Trainer",
                email=f"{trainer_id}@example.com",
                role="trainer",
                specialization="Web Development",
            )
            admin = main.models.User(
                id=admin_id,
                name="Progress Admin",
                email=f"{admin_id}@example.com",
                role="admin",
            )
            student = main.models.User(
                id=student_id,
                name="Progress Student",
                email=f"{student_id}@example.com",
                phone_number="9555555555",
                role="student",
                course_subject="Web Development",
            )
            course = main.models.Course(
                id=course_id,
                title="Progress Course",
                trainer_id=trainer_id,
            )
            first_module = main.models.Module(
                id=str(uuid.uuid4()),
                course_id=course_id,
                order_index=1,
                title="Lesson One",
                video_url="https://example.com/one",
                assignment_prompt="Complete one",
            )
            second_module = main.models.Module(
                id=str(uuid.uuid4()),
                course_id=course_id,
                order_index=2,
                title="Lesson Two",
                video_url="https://example.com/two",
                assignment_prompt="Complete two",
            )
            enrollment = main.models.Enrollment(
                id=enrollment_id,
                student_id=student_id,
                course_id=course_id,
            )
            incomplete_assignment = main.models.StudentProgress(
                id=str(uuid.uuid4()),
                enrollment_id=enrollment_id,
                module_id=first_module.id,
                is_video_completed=True,
                assignment_completed=False,
            )
            complete_lesson = main.models.StudentProgress(
                id=str(uuid.uuid4()),
                enrollment_id=enrollment_id,
                module_id=second_module.id,
                is_video_completed=True,
                assignment_completed=True,
            )
            db.add_all([
                trainer, admin, student, course, first_module, second_module,
                enrollment, incomplete_assignment, complete_lesson,
            ])
            db.commit()

            trainer_token = auth.create_access_token({"sub": trainer_id, "role": "trainer"})
            admin_token = auth.create_access_token({"sub": admin_id, "role": "admin"})
            trainer_response = self.client.get(
                "/trainer/dashboard",
                headers={"Authorization": f"Bearer {trainer_token}"},
            )
            admin_response = self.client.get(
                "/admin/students",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            trainer_student = trainer_response.json()["courses"][0]["students"][0]
            admin_student = next(
                row for row in admin_response.json()["students"]
                if row["student_id"] == student_id
            )

            self.assertEqual(trainer_response.status_code, 200)
            self.assertEqual(admin_response.status_code, 200)
            self.assertEqual(trainer_student["student_id"], student_id)
            self.assertEqual(trainer_student["course_title"], "Progress Course")
            self.assertEqual(trainer_student["completed_slots"], 1)
            self.assertEqual(trainer_student["total_slots"], 2)
            self.assertTrue(trainer_student["enrolled_at"])
            self.assertFalse(trainer_student["modules"][0]["assignment_completed"])
            self.assertEqual(admin_student["student_id"], student_id)
            self.assertEqual(admin_student["phone_number"], "9555555555")
            self.assertEqual(admin_student["completed_slots"], 1)
            self.assertEqual(admin_student["total_slots"], 2)
            self.assertTrue(admin_student["enrolled_at"])
        finally:
            db.close()

    def test_dashboard_queries_are_batched(self):
        db = self.testing_session_local()
        trainer_id = str(uuid.uuid4())
        admin_id = str(uuid.uuid4())
        student_id = str(uuid.uuid4())
        course_id = str(uuid.uuid4())
        try:
            trainer = main.models.User(
                id=trainer_id,
                name="Batch Trainer",
                email=f"{trainer_id}@example.com",
                role="trainer",
                specialization="Web Development",
            )
            admin = main.models.User(
                id=admin_id,
                name="Batch Admin",
                email=f"{admin_id}@example.com",
                role="admin",
            )
            student = main.models.User(
                id=student_id,
                name="Batch Student",
                email=f"{student_id}@example.com",
                role="student",
                course_subject="Web Development",
                is_access_granted=True,
            )
            course = main.models.Course(
                id=course_id,
                title="Batch Course",
                trainer_id=trainer_id,
            )
            modules = [
                main.models.Module(
                    id=str(uuid.uuid4()),
                    course_id=course_id,
                    order_index=order_index,
                    title=f"Lesson {order_index}",
                    video_url=f"https://example.com/lesson-{order_index}",
                    assignment_prompt="Complete this lesson",
                )
                for order_index in range(1, 9)
            ]
            enrollment = main.models.Enrollment(
                id=str(uuid.uuid4()),
                student_id=student_id,
                course_id=course_id,
            )
            db.add_all([trainer, admin, student, course, enrollment, *modules])
            db.commit()

            trainer_headers = {
                "Authorization": f"Bearer {auth.create_access_token({'sub': trainer_id, 'role': 'trainer'})}"
            }
            admin_headers = {
                "Authorization": f"Bearer {auth.create_access_token({'sub': admin_id, 'role': 'admin'})}"
            }
            student_headers = {
                "Authorization": f"Bearer {auth.create_access_token({'sub': student_id, 'role': 'student'})}"
            }

            def query_count_for(request):
                statements = []

                def count_selects(connection, cursor, statement, parameters, context, executemany):
                    if statement.lstrip().upper().startswith("SELECT"):
                        statements.append(statement)

                event.listen(self.engine, "before_cursor_execute", count_selects)
                try:
                    response = request()
                finally:
                    event.remove(self.engine, "before_cursor_execute", count_selects)
                self.assertEqual(response.status_code, 200, response.text)
                return len(statements)

            student_queries = query_count_for(lambda: self.client.get(
                "/student/dashboard",
                headers=student_headers,
            ))
            trainer_queries = query_count_for(lambda: self.client.get(
                "/trainer/dashboard",
                headers=trainer_headers,
            ))
            admin_queries = query_count_for(lambda: self.client.get(
                "/admin/students",
                headers=admin_headers,
            ))

            self.assertLessEqual(student_queries, 4)
            self.assertLessEqual(trainer_queries, 6)
            self.assertLessEqual(admin_queries, 6)
        finally:
            db.close()

    def test_student_cannot_use_another_students_enrollment(self):
        db = self.testing_session_local()
        try:
            trainer_id = str(uuid.uuid4())
            student_id = str(uuid.uuid4())
            other_student_id = str(uuid.uuid4())
            course_id = str(uuid.uuid4())
            other_enrollment_id = str(uuid.uuid4())
            module_id = str(uuid.uuid4())

            trainer = main.models.User(
                id=trainer_id,
                name="Trainer",
                email=f"{trainer_id}@example.com",
                role="trainer",
            )
            student = main.models.User(
                id=student_id,
                name="Student",
                email=f"{student_id}@example.com",
                role="student",
                is_access_granted=True,
            )
            other_student = main.models.User(
                id=other_student_id,
                name="Other Student",
                email=f"{other_student_id}@example.com",
                role="student",
                is_access_granted=True,
            )
            course = main.models.Course(
                id=course_id,
                title="Course",
                trainer_id=trainer_id,
            )
            other_enrollment = main.models.Enrollment(
                id=other_enrollment_id,
                student_id=other_student_id,
                course_id=course_id,
            )
            module = main.models.Module(
                id=module_id,
                course_id=course_id,
                order_index=1,
                title="First lesson",
                video_url="https://example.com/first.mp4",
                assignment_prompt="Complete the task.",
            )
            db.add_all([trainer, student, other_student, course, other_enrollment, module])
            db.commit()

            token = main.auth.create_access_token({"sub": student_id, "role": "student"})
            response = self.client.post(
                f"/student/complete-video/{other_enrollment_id}/{module_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(response.status_code, 404)
        finally:
            db.close()

    def test_course_lesson_questions_are_shared_and_trainer_can_reply(self):
        db = self.testing_session_local()
        try:
            trainer_id = str(uuid.uuid4())
            other_trainer_id = str(uuid.uuid4())
            first_student_id = str(uuid.uuid4())
            second_student_id = str(uuid.uuid4())
            course_id = str(uuid.uuid4())
            first_enrollment_id = str(uuid.uuid4())
            second_enrollment_id = str(uuid.uuid4())
            module_id = str(uuid.uuid4())

            trainer = main.models.User(
                id=trainer_id,
                name="Course Trainer",
                email=f"{trainer_id}@example.com",
                role="trainer",
                specialization="Web Development",
            )
            other_trainer = main.models.User(
                id=other_trainer_id,
                name="Other Trainer",
                email=f"{other_trainer_id}@example.com",
                role="trainer",
            )
            first_student = main.models.User(
                id=first_student_id,
                name="First Student",
                email=f"{first_student_id}@example.com",
                role="student",
                course_subject="Web Development",
                is_access_granted=True,
            )
            second_student = main.models.User(
                id=second_student_id,
                name="Second Student",
                email=f"{second_student_id}@example.com",
                role="student",
                course_subject="Web Development",
                is_access_granted=True,
            )
            course = main.models.Course(
                id=course_id,
                title="Shared Discussion Course",
                trainer_id=trainer_id,
            )
            module = main.models.Module(
                id=module_id,
                course_id=course_id,
                order_index=1,
                title="Discussion Lesson",
                video_url="https://example.com/lesson",
                assignment_prompt="Complete the task.",
            )
            enrollments = [
                main.models.Enrollment(
                    id=first_enrollment_id,
                    student_id=first_student_id,
                    course_id=course_id,
                ),
                main.models.Enrollment(
                    id=second_enrollment_id,
                    student_id=second_student_id,
                    course_id=course_id,
                ),
            ]
            db.add_all([
                trainer, other_trainer, first_student, second_student,
                course, module, *enrollments,
            ])
            db.commit()

            first_student_headers = {
                "Authorization": f"Bearer {auth.create_access_token({'sub': first_student_id, 'role': 'student'})}",
            }
            second_student_headers = {
                "Authorization": f"Bearer {auth.create_access_token({'sub': second_student_id, 'role': 'student'})}",
            }
            trainer_headers = {
                "Authorization": f"Bearer {auth.create_access_token({'sub': trainer_id, 'role': 'trainer'})}",
            }
            other_trainer_headers = {
                "Authorization": f"Bearer {auth.create_access_token({'sub': other_trainer_id, 'role': 'trainer'})}",
            }

            question_response = self.client.post(
                f"/student/enrollments/{first_enrollment_id}/modules/{module_id}/questions",
                json={"question": "How does this lesson's example work?"},
                headers=first_student_headers,
            )
            self.assertEqual(question_response.status_code, 201, question_response.text)
            question_id = question_response.json()["id"]

            second_student_dashboard = self.client.get(
                "/student/dashboard",
                headers=second_student_headers,
            )
            trainer_dashboard = self.client.get(
                "/trainer/dashboard",
                headers=trainer_headers,
            )
            self.assertEqual(second_student_dashboard.status_code, 200)
            self.assertEqual(
                second_student_dashboard.json()["courses"][0]["modules"][0]["questions"][0]["question"],
                "How does this lesson's example work?",
            )
            self.assertEqual(trainer_dashboard.status_code, 200)
            self.assertEqual(
                trainer_dashboard.json()["courses"][0]["modules"][0]["questions"][0]["id"],
                question_id,
            )

            unauthorized_reply = self.client.post(
                f"/trainer/lesson-questions/{question_id}/reply",
                json={"reply": "Unauthorized response"},
                headers=other_trainer_headers,
            )
            student_reply = self.client.post(
                f"/trainer/lesson-questions/{question_id}/reply",
                json={"reply": "Students cannot reply as the trainer"},
                headers=first_student_headers,
            )
            reply_response = self.client.post(
                f"/trainer/lesson-questions/{question_id}/reply",
                json={"reply": "The example uses a callback to handle the result."},
                headers=trainer_headers,
            )
            self.assertEqual(unauthorized_reply.status_code, 403)
            self.assertEqual(student_reply.status_code, 403)
            self.assertEqual(reply_response.status_code, 200, reply_response.text)
            self.assertEqual(
                reply_response.json()["trainer_reply"],
                "The example uses a callback to handle the result.",
            )

            updated_student_dashboard = self.client.get(
                "/student/dashboard",
                headers=second_student_headers,
            )
            self.assertEqual(
                updated_student_dashboard.json()["courses"][0]["modules"][0]["questions"][0]["trainer_reply"],
                "The example uses a callback to handle the result.",
            )
        finally:
            db.close()

    def test_trainer_can_only_evaluate_their_own_pending_assignments(self):
        db = self.testing_session_local()
        try:
            trainer_id = str(uuid.uuid4())
            other_trainer_id = str(uuid.uuid4())
            student_id = str(uuid.uuid4())
            course_id = str(uuid.uuid4())
            enrollment_id = str(uuid.uuid4())
            module_id = str(uuid.uuid4())
            progress_id = str(uuid.uuid4())
            trainer = main.models.User(
                id=trainer_id,
                name="Trainer",
                email=f"{trainer_id}@example.com",
                role="trainer",
            )
            other_trainer = main.models.User(
                id=other_trainer_id,
                name="Other Trainer",
                email=f"{other_trainer_id}@example.com",
                role="trainer",
            )
            student = main.models.User(
                id=student_id,
                name="Student",
                email=f"{student_id}@example.com",
                role="student",
                is_access_granted=True,
            )
            course = main.models.Course(
                id=course_id,
                title="Course",
                trainer_id=trainer_id,
            )
            enrollment = main.models.Enrollment(
                id=enrollment_id,
                student_id=student_id,
                course_id=course_id,
            )
            module = main.models.Module(
                id=module_id,
                course_id=course_id,
                order_index=1,
                title="Lesson",
                video_url="https://example.com/video.mp4",
                assignment_prompt="Complete the task.",
            )
            progress = main.models.StudentProgress(
                id=progress_id,
                enrollment_id=enrollment_id,
                module_id=module_id,
                is_video_completed=True,
                assignment_status="submitted",
            )
            db.add_all([trainer, other_trainer, student, course, enrollment, module, progress])
            db.commit()

            payload = {
                "progress_id": progress_id,
                "is_approved": True,
                "feedback": "Good work",
            }
            other_token = auth.create_access_token({"sub": other_trainer_id, "role": "trainer"})
            other_response = self.client.post(
                "/trainer/evaluate-assignment",
                json=payload,
                headers={"Authorization": f"Bearer {other_token}"},
            )
            owner_token = auth.create_access_token({"sub": trainer_id, "role": "trainer"})
            owner_response = self.client.post(
                "/trainer/evaluate-assignment",
                json=payload,
                headers={"Authorization": f"Bearer {owner_token}"},
            )
            duplicate_response = self.client.post(
                "/trainer/evaluate-assignment",
                json=payload,
                headers={"Authorization": f"Bearer {owner_token}"},
            )

            self.assertEqual(other_response.status_code, 403)
            self.assertEqual(owner_response.status_code, 200)
            self.assertEqual(duplicate_response.status_code, 409)
        finally:
            db.close()

    def test_password_login_is_rate_limited(self):
        email = f"{uuid.uuid4()}@example.com"
        response = None
        for _ in range(auth.PASSWORD_LOGIN_LIMIT):
            response = self.client.post(
                "/auth/login-password",
                json={"email": email, "password": "wrong-password"},
            )
            self.assertEqual(response.status_code, 401)

        response = self.client.post(
            "/auth/login-password",
            json={"email": email, "password": "wrong-password"},
        )
        self.assertEqual(response.status_code, 429)

    def test_password_hash_does_not_truncate_at_bcrypt_limit(self):
        first_password = "a" * 72 + "first"
        second_password = "a" * 72 + "second"
        hashed = auth.hash_password(first_password)

        self.assertTrue(auth.verify_password(first_password, hashed))
        self.assertFalse(auth.verify_password(second_password, hashed))
        legacy_hash = auth.pwd_context.hash("legacy-password")
        self.assertTrue(auth.verify_password("legacy-password", legacy_hash))
        self.assertTrue(auth.is_legacy_password_hash(legacy_hash))
        self.assertFalse(auth.is_legacy_password_hash(hashed))

    def test_legacy_password_hash_is_upgraded_after_successful_login(self):
        db = self.testing_session_local()
        user_id = str(uuid.uuid4())
        legacy_hash = auth.pwd_context.hash("legacy-password")
        user = main.models.User(
            id=user_id,
            name="Legacy User",
            email=f"{user_id}@example.com",
            role="student",
            hashed_password=legacy_hash,
        )
        db.add(user)
        db.commit()
        db.close()

        response = self.client.post(
            "/auth/login-password",
            json={"email": f"{user_id}@example.com", "password": "legacy-password"},
        )

        db = self.testing_session_local()
        try:
            stored_user = db.get(main.models.User, user_id)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(stored_user.hashed_password.startswith("sha256$"))
            self.assertTrue(auth.verify_password("legacy-password", stored_user.hashed_password))
        finally:
            db.close()

    def test_otp_is_persisted_and_burned_after_failed_attempt_limit(self):
        db = self.testing_session_local()
        phone = f"9{uuid.uuid4().int % 1000000000:09d}"
        otp_security.save_otp(db, phone, "123456")
        db.close()
        db = self.testing_session_local()
        try:
            for _ in range(otp_security.MAX_VERIFY_ATTEMPTS):
                valid, _ = otp_security.verify_and_burn_otp(db, phone, "000000")
                self.assertFalse(valid)

            valid, message = otp_security.verify_and_burn_otp(db, phone, "123456")
            self.assertFalse(valid)
            self.assertIn("expired", message.lower())
        finally:
            otp_security.discard_otp(db, phone)
            db.close()

    def test_sms_otp_uses_sms_alert_api(self):
        import sms_service

        class SuccessfulResponse:
            status = 202

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, size):
                return b'{"status":"accepted"}'

        with (
            patch.object(sms_service.settings, "SMS_ALERT_API_KEY", "unit-test-api-key"),
            patch.object(sms_service.settings, "SMS_ALERT_SENDER_ID", "LMSOTP"),
            patch.object(
                sms_service.settings,
                "SMS_ALERT_OTP_MESSAGE",
                "Your verification code is {otp}.",
            ),
            patch("sms_service.urlopen", return_value=SuccessfulResponse()) as send_request,
        ):
            sms_service.send_sms_otp("9876543210", "123456")

        request = send_request.call_args.args[0]
        params = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(request.method, "POST")
        self.assertEqual(params["apikey"], ["unit-test-api-key"])
        self.assertEqual(params["sender"], ["LMSOTP"])
        self.assertEqual(params["mobileno"], ["+919876543210"])
        self.assertEqual(params["text"], ["Your verification code is 123456."])
        self.assertEqual(send_request.call_args.kwargs["timeout"], 10)

    def test_sms_alert_configuration_requires_otp_template_placeholder(self):
        import sms_service

        with (
            patch.object(sms_service.settings, "SMS_ALERT_API_KEY", "unit-test-api-key"),
            patch.object(sms_service.settings, "SMS_ALERT_SENDER_ID", "LMSOTP"),
            patch.object(
                sms_service.settings,
                "SMS_ALERT_OTP_MESSAGE",
                "Your code is {otp}.",
            ),
        ):
            self.assertTrue(sms_service.is_sms_delivery_configured())

        with (
            patch.object(sms_service.settings, "SMS_ALERT_API_KEY", "unit-test-api-key"),
            patch.object(sms_service.settings, "SMS_ALERT_SENDER_ID", "LMSOTP"),
            patch.object(
                sms_service.settings,
                "SMS_ALERT_OTP_MESSAGE",
                "Your verification code is ready.",
            ),
        ):
            self.assertFalse(sms_service.is_sms_delivery_configured())

    def test_sms_alert_http_error_has_safe_actionable_diagnostics(self):
        import sms_service
        from urllib.error import HTTPError

        with (
            patch.object(sms_service.settings, "SMS_ALERT_API_KEY", "unit-test-api-key"),
            patch.object(sms_service.settings, "SMS_ALERT_SENDER_ID", "LMSOTP"),
            patch("sms_service.urlopen", side_effect=HTTPError(
                "https://www.smsalert.co.in/api/push.json?apikey=unit-test-api-key",
                401,
                "Unauthorized",
                {},
                None,
            )),
            self.assertRaises(sms_service.SmsAlertError) as raised,
        ):
            sms_service.send_sms_otp("9876543210", "123456")

        self.assertIn("HTTP 401", str(raised.exception))
        self.assertIn("API key was rejected", str(raised.exception))
        self.assertNotIn("unit-test-api-key", str(raised.exception))
        self.assertNotIn("123456", str(raised.exception))

    def test_sms_alert_json_error_response_is_not_reported_as_success(self):
        import sms_service

        class RejectedResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, size):
                return b'{"status":"error","description":"sender rejected"}'

        with (
            patch.object(sms_service.settings, "SMS_ALERT_API_KEY", "unit-test-api-key"),
            patch.object(sms_service.settings, "SMS_ALERT_SENDER_ID", "LMSOTP"),
            patch("sms_service.urlopen", return_value=RejectedResponse()),
            self.assertRaises(sms_service.SmsAlertError) as raised,
        ):
            sms_service.send_sms_otp("9876543210", "123456")

        self.assertIn("did not accept", str(raised.exception))
        self.assertNotIn("123456", str(raised.exception))

    def test_seed_script_provisions_hashed_role_accounts_idempotently(self):
        db = self.testing_session_local()
        email = f"{uuid.uuid4()}@example.com"
        try:
            user = seed_db.seed_role_account(
                db,
                email,
                "Seeded Trainer",
                "trainer",
                "TrainerPassword-First",
            )
            first_hash = user.hashed_password
            self.assertNotEqual(first_hash, "TrainerPassword-First")
            self.assertTrue(auth.verify_password("TrainerPassword-First", first_hash))

            updated_user = seed_db.seed_role_account(
                db,
                email,
                "Seeded Trainer",
                "trainer",
                "TrainerPassword-Updated",
            )
            self.assertEqual(user.id, updated_user.id)
            self.assertTrue(auth.verify_password(
                "TrainerPassword-Updated",
                updated_user.hashed_password,
            ))
            self.assertFalse(auth.verify_password(
                "TrainerPassword-First",
                updated_user.hashed_password,
            ))
        finally:
            db.close()

    def test_seed_script_loads_editable_admin_and_trainer_accounts(self):
        accounts = [
            {
                "role": "admin",
                "name": "Admin",
                "specialization": "Platform operations",
                "email": "admin@example.com",
                "password": "test-admin-password",
            },
            {
                "role": "trainer",
                "name": "Trainer",
                "specialization": "Web development",
                "email": "trainer@example.com",
                "password": "test-trainer-password",
            },
        ]
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as config_file:
            json.dump(accounts, config_file)
            path = config_file.name
        try:
            self.assertEqual(seed_db.load_seed_accounts(path), accounts)
        finally:
            os.unlink(path)

    def test_otp_request_does_not_reveal_if_phone_is_registered(self):
        db = self.testing_session_local()
        phone = f"9{uuid.uuid4().int % 1000000000:09d}"
        user_id = str(uuid.uuid4())
        db.add(main.models.User(
            id=user_id,
            name="OTP Student",
            email=f"{user_id}@example.com",
            phone_number=phone,
            role="student",
            is_access_granted=True,
        ))
        db.commit()
        try:
            with (
                patch.object(main, "is_sms_delivery_configured", return_value=True),
                patch.object(main, "send_sms_otp") as send_sms,
            ):
                registered_response = self.client.post(
                    "/auth/request-otp",
                    json={"phone_number": phone},
                )
                unregistered_response = self.client.post(
                    "/auth/request-otp",
                    json={"phone_number": "9876543210"},
                )

            self.assertEqual(registered_response.status_code, 200)
            self.assertEqual(unregistered_response.status_code, 200)
            self.assertIn("If this number is registered", registered_response.json()["message"])
            self.assertIn("delivery report", registered_response.json()["message"])
            self.assertEqual(registered_response.json(), unregistered_response.json())
            send_sms.assert_called_once()
        finally:
            otp_security.discard_otp(db, phone)
            db.close()

    def test_sms_otp_request_and_verification_work_end_to_end(self):
        phone = f"9{uuid.uuid4().int % 1000000000:09d}"
        user_id = str(uuid.uuid4())
        db = self.testing_session_local()
        db.add(main.models.User(
            id=user_id,
            name="SMS Login Student",
            email=f"{user_id}@example.com",
            phone_number=phone,
            role="student",
            is_access_granted=True,
        ))
        db.commit()
        db.close()

        with (
            patch.object(main, "is_sms_delivery_configured", return_value=True),
            patch.object(main, "generate_secure_otp", return_value="246810"),
            patch.object(main, "send_sms_otp") as send_sms,
            patch.object(main.auth, "record_rate_limit_attempt", return_value=True),
        ):
            request_response = self.client.post(
                "/auth/request-otp",
                json={"phone_number": phone},
            )
            verify_response = self.client.post(
                "/auth/verify-otp",
                json={"phone_number": phone, "otp_code": "246810"},
            )

        self.assertEqual(request_response.status_code, 200, request_response.text)
        self.assertEqual(verify_response.status_code, 200, verify_response.text)
        self.assertEqual(verify_response.json()["role"], "student")
        self.assertTrue(verify_response.json()["access_token"])
        send_sms.assert_called_once_with(phone, "246810")
        db = self.testing_session_local()
        try:
            self.assertIsNone(db.get(main.models.OTPChallenge, otp_security._phone_hash(phone)))
        finally:
            db.close()

    def test_dev_mode_returns_otp_without_sending_sms(self):
        phone = f"9{uuid.uuid4().int % 1000000000:09d}"
        user_id = str(uuid.uuid4())
        db = self.testing_session_local()
        db.add(main.models.User(
            id=user_id,
            name="Development OTP Student",
            email=f"{user_id}@example.com",
            phone_number=phone,
            role="student",
            is_access_granted=True,
        ))
        db.commit()
        db.close()

        with (
            patch.object(main.settings, "DEV_MODE", True),
            patch.object(main, "is_sms_delivery_configured", return_value=False),
            patch.object(main, "generate_secure_otp", return_value="135790"),
            patch.object(main, "send_sms_otp") as send_sms,
            patch.object(main.auth, "record_rate_limit_attempt", return_value=True),
        ):
            request_response = self.client.post(
                "/auth/request-otp",
                json={"phone_number": phone},
            )
            verify_response = self.client.post(
                "/auth/verify-otp",
                json={"phone_number": phone, "otp_code": "135790"},
            )

        self.assertEqual(request_response.status_code, 200, request_response.text)
        self.assertEqual(request_response.json()["development_otp"], "135790")
        self.assertIn("Development mode", request_response.json()["message"])
        self.assertEqual(verify_response.status_code, 200, verify_response.text)
        self.assertEqual(verify_response.json()["role"], "student")
        send_sms.assert_not_called()

    def test_otp_request_reports_missing_sms_configuration_without_enumerating_users(self):
        phone = f"9{uuid.uuid4().int % 1000000000:09d}"
        user_id = str(uuid.uuid4())
        db = self.testing_session_local()
        db.add(main.models.User(
            id=user_id,
            name="SMS Student",
            email=f"{user_id}@example.com",
            phone_number=phone,
            role="student",
            is_access_granted=True,
        ))
        db.commit()
        db.close()

        with patch.object(main, "is_sms_delivery_configured", return_value=False):
            registered_response = self.client.post(
                "/auth/request-otp",
                json={"phone_number": phone},
            )
            unregistered_response = self.client.post(
                "/auth/request-otp",
                json={"phone_number": "9876543210"},
            )

        self.assertEqual(registered_response.status_code, 503)
        self.assertEqual(unregistered_response.status_code, 503)
        self.assertEqual(registered_response.json(), unregistered_response.json())

    def test_otp_delivery_failure_invalidates_generated_code(self):
        phone = f"9{uuid.uuid4().int % 1000000000:09d}"
        user_id = str(uuid.uuid4())
        db = self.testing_session_local()
        db.add(main.models.User(
            id=user_id,
            name="SMS Student",
            email=f"{user_id}@example.com",
            phone_number=phone,
            role="student",
            is_access_granted=True,
        ))
        db.commit()
        db.close()

        with (
            patch.object(main, "is_sms_delivery_configured", return_value=True),
            patch.object(main, "send_sms_otp", side_effect=main.SmsAlertError("provider error")),
        ):
            response = self.client.post(
                "/auth/request-otp",
                json={"phone_number": phone},
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("provider error", response.json()["detail"])
        db = self.testing_session_local()
        try:
            valid, message = otp_security.verify_and_burn_otp(db, phone, "000000")
            self.assertFalse(valid)
            self.assertIn("expired", message.lower())
        finally:
            otp_security.discard_otp(db, phone)
            db.close()

    def test_password_reset_with_otp_supports_students_and_trainers(self):
        users = []
        db = self.testing_session_local()
        for role in ("student", "trainer"):
            phone = f"9{uuid.uuid4().int % 1000000000:09d}"
            user_id = str(uuid.uuid4())
            user = main.models.User(
                id=user_id,
                name=f"Reset {role}",
                email=f"{user_id}@example.com",
                phone_number=phone,
                role=role,
                hashed_password=auth.get_password_hash("OldPassword123"),
            )
            db.add(user)
            otp_security.save_otp(db, phone, "246810")
            users.append((phone, role))
        db.close()

        with patch.object(main.auth, "record_rate_limit_attempt", return_value=True):
            for phone, role in users:
                response = self.client.post(
                    "/auth/reset-password",
                    json={
                        "phone_number": phone,
                        "otp_code": "246810",
                        "new_password": "NewPassword456",
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("reset successfully", response.json()["message"])

                db = self.testing_session_local()
                try:
                    user = db.query(main.models.User).filter_by(
                        phone_number=phone,
                        role=role,
                    ).one()
                    self.assertTrue(auth.verify_password("NewPassword456", user.hashed_password))
                    self.assertFalse(auth.verify_password("OldPassword123", user.hashed_password))
                finally:
                    db.close()

    def test_password_reset_rejects_admin_accounts(self):
        phone = f"9{uuid.uuid4().int % 1000000000:09d}"
        user_id = str(uuid.uuid4())
        old_password_hash = auth.get_password_hash("OldPassword123")
        db = self.testing_session_local()
        db.add(main.models.User(
            id=user_id,
            name="Reset Admin",
            email=f"{user_id}@example.com",
            phone_number=phone,
            role="admin",
            hashed_password=old_password_hash,
        ))
        otp_security.save_otp(db, phone, "246810")
        db.close()

        with patch.object(main.auth, "record_rate_limit_attempt", return_value=True):
            response = self.client.post(
                "/auth/reset-password",
                json={
                    "phone_number": phone,
                    "otp_code": "246810",
                    "new_password": "NewPassword456",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid or expired", response.json()["detail"])
        db = self.testing_session_local()
        try:
            user = db.query(main.models.User).filter_by(id=user_id).one()
            self.assertEqual(user.hashed_password, old_password_hash)
        finally:
            otp_security.discard_otp(db, phone)
            db.close()

    def test_existing_trainer_can_register_recovery_phone_and_reset_password(self):
        user_id = str(uuid.uuid4())
        phone = f"9{uuid.uuid4().int % 1000000000:09d}"
        db = self.testing_session_local()
        db.add(main.models.User(
            id=user_id,
            name="Trainer Without Recovery Phone",
            email=f"{user_id}@example.com",
            role="trainer",
            hashed_password=auth.get_password_hash("OldPassword123"),
        ))
        db.commit()
        db.close()
        headers = {
            "Authorization": f"Bearer {auth.create_access_token({'sub': user_id, 'role': 'trainer'})}",
        }

        with (
            patch.object(main.settings, "DEV_MODE", True),
            patch.object(main.auth, "record_rate_limit_attempt", return_value=True),
            patch.object(main, "generate_secure_otp", return_value="123456"),
        ):
            otp_response = self.client.post(
                "/auth/request-recovery-phone-otp",
                json={"phone_number": phone},
                headers=headers,
            )
            save_response = self.client.put(
                "/user/recovery-phone",
                json={"phone_number": phone, "otp_code": "123456"},
                headers=headers,
            )

        self.assertEqual(otp_response.status_code, 200, otp_response.text)
        self.assertEqual(otp_response.json()["development_otp"], "123456")
        self.assertEqual(save_response.status_code, 200, save_response.text)

        db = self.testing_session_local()
        try:
            user = db.query(main.models.User).filter_by(id=user_id).one()
            self.assertEqual(user.phone_number, phone)
            otp_security.save_otp(db, phone, "654321")
        finally:
            db.close()

        reset_response = self.client.post(
            "/auth/reset-password",
            json={
                "phone_number": phone,
                "otp_code": "654321",
                "new_password": "NewPassword456",
            },
        )
        self.assertEqual(reset_response.status_code, 200, reset_response.text)
        db = self.testing_session_local()
        try:
            user = db.query(main.models.User).filter_by(id=user_id).one()
            self.assertTrue(auth.verify_password("NewPassword456", user.hashed_password))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
