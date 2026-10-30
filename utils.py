import os
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

PROJECT_ASSIGNMENT_COUNT = 10
PROJECT_ASSIGNMENT_LINKS_BY_LESSON = {
    lesson_order: tuple(
        f"https://example.com/projects/lesson-{lesson_order:02d}/project-{project_number:02d}"
        for project_number in range(1, PROJECT_ASSIGNMENT_COUNT + 1)
    )
    for lesson_order in range(1, 13)
}


def get_project_assignment_link(lesson_order: int, project_index: int | None) -> str | None:
    if project_index is None:
        return None
    lesson_links = PROJECT_ASSIGNMENT_LINKS_BY_LESSON.get(lesson_order)
    if lesson_links:
        return lesson_links[project_index % len(lesson_links)]
    return None


def generate_pdf_certificate(student_name: str, course_name: str, cert_id: str) -> str:
    """Generates a downloadable PDF certificate and saves it locally."""
    certs_dir = os.path.join(os.getcwd(), "certificates")
    os.makedirs(certs_dir, exist_ok=True)
    file_path = os.path.join(certs_dir, f"{cert_id}.pdf")

    c = canvas.Canvas(file_path, pagesize=letter)
    width, height = letter

    # Outer decorative border
    c.setLineWidth(4)
    c.setStrokeColorRGB(0.1, 0.2, 0.4)
    c.rect(30, 30, width - 60, height - 60)

    # Inner thin border
    c.setLineWidth(1)
    c.rect(36, 36, width - 72, height - 72)

    # Titles & Text
    c.setFont("Helvetica-Bold", 26)
    c.setFillColorRGB(0.1, 0.2, 0.4)
    c.drawCentredString(width / 2.0, height - 150, "CERTIFICATE OF COMPLETION")

    c.setFont("Helvetica", 14)
    c.setFillColorRGB(0.3, 0.3, 0.3)
    c.drawCentredString(width / 2.0, height - 210, "This is proudly presented to")

    c.setFont("Helvetica-Bold", 22)
    c.setFillColorRGB(0.0, 0.0, 0.0)
    c.drawCentredString(width / 2.0, height - 260, student_name)

    c.setFont("Helvetica", 14)
    c.setFillColorRGB(0.3, 0.3, 0.3)
    c.drawCentredString(width / 2.0, height - 310, "for successfully finishing all lessons and assignments in:")

    c.setFont("Helvetica-Bold", 18)
    c.setFillColorRGB(0.1, 0.2, 0.4)
    c.drawCentredString(width / 2.0, height - 350, course_name)

    c.setFont("Helvetica-Oblique", 10)
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.drawCentredString(
        width / 2.0,
        height - 430,
        f"Certificate ID: {cert_id} | Issued Date: {datetime.utcnow().strftime('%B %d, %Y')}"
    )

    c.showPage()
    c.save()
    return file_path