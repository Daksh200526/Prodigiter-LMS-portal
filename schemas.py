from pydantic import BaseModel, EmailStr, Field
from typing import Dict, List, Optional

class LoginPassword(BaseModel):
    email: EmailStr
    password: str

class StudentRegister(BaseModel):
    name: str
    email: EmailStr
    phone_number: str
    password: str
    course_subject: str

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
    order_index: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=255)
    video_url: str = Field(min_length=1, max_length=500)
    assignment_prompt: str = Field(min_length=1)
    assignment_link: Optional[str] = Field(default=None, max_length=500)

class CourseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    modules: List[ModuleCreate] = Field(min_length=1)

class AssignmentEvaluation(BaseModel):
    progress_id: str
    is_approved: bool
    feedback: Optional[str] = ""

class LessonQuestionCreate(BaseModel):
    question: str = Field(min_length=1, max_length=2000)

class LessonQuestionReply(BaseModel):
    reply: str = Field(min_length=1, max_length=2000)

class TaskReviewAnswers(BaseModel):
    answers: Dict[str, str]

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