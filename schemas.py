from pydantic import BaseModel, EmailStr, Field
from typing import Dict, List, Literal, Optional

class LoginPassword(BaseModel):
    email: EmailStr
    password: str

class StudentRegister(BaseModel):
    name: str
    email: EmailStr
    phone_number: str
    password: str

class OTPRequest(BaseModel):
    phone_number: str

class OTPVerify(BaseModel):
    phone_number: str
    otp_code: str

class PasswordReset(BaseModel):
    phone_number: str
    otp_code: str
    new_password: str = Field(min_length=8, max_length=1024)

class ModuleCreate(BaseModel):
    order_index: int = Field(ge=1, le=12)
    title: str = Field(min_length=1, max_length=255)
    video_url: str = Field(default="", max_length=500)
    assignment_prompt: Optional[str] = None

class CourseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    modules: List[ModuleCreate] = Field(min_length=1)

class CourseAccessDecision(BaseModel):
    approved: bool

class CourseAccessRequestCreate(BaseModel):
    course_id: str

class StudentMajorUpdate(BaseModel):
    course_subject: str

class AssignmentEvaluation(BaseModel):
    progress_id: str
    is_approved: bool
    feedback: Optional[str] = ""

class LessonQuestionCreate(BaseModel):
    question: str = Field(min_length=1, max_length=2000)

class LessonQuestionReply(BaseModel):
    reply: str = Field(min_length=1, max_length=2000)

class AnnouncementCreate(BaseModel):
    title: str = Field(min_length=1, max_length=150)
    message: str = Field(min_length=1, max_length=4000)

class AnnouncementReadRequest(BaseModel):
    announcement_ids: List[str] = Field(min_length=1, max_length=1000)

class TaskReviewAnswers(BaseModel):
    answers: Dict[str, str]

class DayOneVisit(BaseModel):
    enrollment_id: str
    module_id: str
    website_index: int = Field(ge=0, le=4)

class DayOneAnswer(BaseModel):
    enrollment_id: str
    module_id: str
    website_index: int = Field(ge=0, le=4)
    question_index: int = Field(ge=0, le=8)
    answer: Literal["yes", "no", "not_applicable"]

class DailyCompetitorDetails(BaseModel):
    business_name: str = Field(min_length=1, max_length=255)
    address: str = Field(min_length=1, max_length=1000)
    contact_number: str = Field(min_length=1, max_length=100)
    business_category: str = Field(min_length=1, max_length=255)
    website_url: str = Field(min_length=1, max_length=1000)
    reviews: str = Field(min_length=1, max_length=255)
    rating: str = Field(min_length=1, max_length=100)
    photo_count: str = Field(min_length=1, max_length=100)

class DailyCompetitorProfile(DailyCompetitorDetails):
    profile_type: Literal["business", "competitor"]

class DailySocialPost(BaseModel):
    image_description: str = Field(min_length=1, max_length=2000)
    content: str = Field(min_length=1, max_length=4000)
    cta: str = Field(min_length=1, max_length=500)

class DailyTaskSubmission(BaseModel):
    enrollment_id: str
    module_id: str
    task: Literal["rankings", "competitor", "posts"]
    rankings: Optional[List[str]] = Field(default=None, min_length=14, max_length=14)
    competitor_profiles: Optional[List[DailyCompetitorProfile]] = Field(
        default=None,
        min_length=2,
        max_length=10,
    )
    posts: Optional[List[DailySocialPost]] = Field(default=None, min_length=3, max_length=3)

class DeletionProcess(BaseModel):
    request_id: str
    approved: bool

class TrainerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    phone_number: str = Field(min_length=1, max_length=20)
    specialization: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=1024)

class TrainerDeletionRequest(BaseModel):
    student_id: str
    reason: str = Field(min_length=1, max_length=2000)

class ProfileUpdate(BaseModel):
    bio: Optional[str] = None