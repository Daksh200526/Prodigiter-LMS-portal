HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Enterprise LMS Portal</title>
  <style>
    :root {
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --bg: #f8fafc;
      --card: #ffffff;
      --text: #0f172a;
      --border: #e2e8f0;
      --sidebar-w: 320px;
    }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      background: #ffffff;
      border-bottom: 1px solid var(--border);
      padding: 0.85rem 2rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 50;
    }
    .nav-actions {
      display: flex;
      align-items: center;
      gap: 1rem;
    }
    .profile-link-btn {
      background: #f1f5f9;
      color: #1e293b;
      border: 1px solid var(--border);
      padding: 0.45rem 0.9rem;
      border-radius: 6px;
      font-weight: 600;
      cursor: pointer;
      font-size: 0.9rem;
    }
    .profile-link-btn:hover {
      background: #e2e8f0;
    }
    .logout-btn {
      background: #fee2e2;
      color: #991b1b;
      border: 1px solid #fecaca;
      padding: 0.45rem 0.9rem;
      border-radius: 6px;
      font-weight: 600;
      cursor: pointer;
      font-size: 0.9rem;
    }
    .logout-btn:hover {
      background: #fca5a5;
    }
    .container {
      max-width: 1200px;
      margin: 2rem auto;
      padding: 0 1rem;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1.5rem;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }
    .role-tabs {
      display: flex;
      gap: 0.35rem;
      margin-bottom: 1.5rem;
      border-bottom: 1px solid var(--border);
      padding-bottom: 0.5rem;
      flex-wrap: wrap;
    }
    .tab-btn {
      background: none;
      border: none;
      padding: 0.5rem 0.8rem;
      font-weight: 600;
      cursor: pointer;
      color: #64748b;
      border-radius: 4px;
      font-size: 0.9rem;
    }
    .tab-btn.active {
      background: #eff6ff;
      color: var(--primary);
    }
    .form-group {
      margin-bottom: 1rem;
    }
    label {
      display: block;
      margin-bottom: 0.25rem;
      font-weight: 500;
      font-size: 0.9rem;
    }
    input {
      width: 100%;
      padding: 0.6rem;
      box-sizing: border-box;
      border: 1px solid var(--border);
      border-radius: 4px;
      font-size: 1rem;
    }
    button.submit-btn {
      width: 100%;
      background: var(--primary);
      color: white;
      border: none;
      padding: 0.75rem;
      font-size: 1rem;
      font-weight: 600;
      border-radius: 4px;
      cursor: pointer;
      margin-top: 0.5rem;
    }
    button.submit-btn:hover {
      background: var(--primary-hover);
    }
    .bottom-switch-link {
      text-align: center;
      margin-top: 1.5rem;
      font-size: 0.92rem;
      color: #64748b;
    }
    .bottom-switch-link a {
      color: var(--primary);
      text-decoration: none;
      font-weight: 600;
      cursor: pointer;
    }
    .bottom-switch-link a:hover {
      text-decoration: underline;
    }
    .hidden {
      display: none !important;
    }
    .badge {
      display: inline-block;
      padding: 0.2rem 0.5rem;
      border-radius: 4px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .badge-success { background: #dcfce7; color: #166534; }
    .badge-warning { background: #fef3c7; color: #854d0e; }
    .badge-neutral { background: #e2e8f0; color: #334155; }
    .badge-danger { background: #fee2e2; color: #991b1b; }

    /* Student Workspace Layout */
    .student-layout {
      display: flex;
      gap: 1.5rem;
      align-items: flex-start;
    }
    .lessons-sidebar {
      width: var(--sidebar-w);
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      flex-shrink: 0;
    }
    .sidebar-header {
      padding: 1rem;
      background: #f8fafc;
      border-bottom: 1px solid var(--border);
      font-weight: bold;
    }
    .lesson-item {
      padding: 0.85rem 1rem;
      border-bottom: 1px solid var(--border);
      cursor: pointer;
      transition: background 0.15s;
    }
    .lesson-item:hover {
      background: #f1f5f9;
    }
    .lesson-item.active {
      background: #eff6ff;
      border-left: 4px solid var(--primary);
    }
    .lesson-item.locked {
      opacity: 0.65;
      cursor: not-allowed;
      background: #fafafa;
    }
    .video-workspace {
      flex: 1;
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1.5rem;
    }
    .video-container {
      position: relative;
      padding-bottom: 56.25%;
      height: 0;
      overflow: hidden;
      background: #000;
      border-radius: 6px;
      margin-bottom: 1.25rem;
    }
    .video-container iframe, .video-container video {
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      border: none;
    }
    .assignment-card {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 1.25rem;
      margin-top: 1rem;
    }
    .assignment-link-btn {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      background: #ffffff;
      border: 1px solid var(--border);
      padding: 0.65rem 1rem;
      border-radius: 6px;
      text-decoration: none;
      color: var(--primary);
      font-weight: 600;
      margin-top: 0.5rem;
      margin-bottom: 1rem;
    }
    .assignment-link-btn:hover {
      background: #eff6ff;
    }

    /* Modal Backdrop */
    .modal-backdrop {
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: rgba(0, 0, 0, 0.45);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 100;
    }
    .modal-box {
      background: white;
      border-radius: 8px;
      width: 100%;
      max-width: 460px;
      padding: 1.5rem;
      box-shadow: 0 10px 25px rgba(0,0,0,0.15);
    }
    .modal-close-btn {
      float: right;
      background: none;
      border: none;
      font-size: 1.25rem;
      cursor: pointer;
      color: #64748b;
    }
  </style>
</head>
<body>

<header>
  <h2 style="margin: 0; font-size: 1.25rem;">Enterprise LMS</h2>
  <div class="nav-actions">
    <button id="profile-btn" class="profile-link-btn hidden" onclick="openProfileModal()">Student Profile</button>
    <button id="logout-btn" class="logout-btn hidden" onclick="handleLogout()">Logout</button>
  </div>
</header>

<!-- Student Profile Modal -->
<div id="profile-modal" class="modal-backdrop hidden">
  <div class="modal-box">
    <button class="modal-close-btn" onclick="closeProfileModal()">&times;</button>
    <h3 style="margin-top: 0;">Student Profile</h3>
    <hr style="border: 0; border-top: 1px solid var(--border); margin: 1rem 0;">
    <div id="profile-details-content">Loading profile...</div>
  </div>
</div>

<div class="container">
  <!-- Auth Section -->
  <div id="auth-section" class="card" style="max-width: 500px; margin: 0 auto;">
    
    <!-- Login Views Wrapper -->
    <div id="login-container">
      <div class="role-tabs">
        <button id="tab-admin" class="tab-btn active" onclick="switchLoginRole('admin')">Admin</button>
        <button id="tab-trainer" class="tab-btn" onclick="switchLoginRole('trainer')">Trainer</button>
        <button id="tab-student" class="tab-btn" onclick="switchLoginRole('student')">Student Password</button>
        <button id="tab-otp" class="tab-btn" onclick="switchLoginRole('student-otp')">Student OTP Login</button>
      </div>

      <!-- Password Login Form -->
      <div id="password-login-box">
        <div class="form-group">
          <label>Email Address</label>
          <input type="email" id="login-email" placeholder="name@lms.com" autocomplete="off" value="">
        </div>
        <div class="form-group">
          <label>Password</label>
          <input type="password" id="login-password" placeholder="••••••••" autocomplete="new-password" value="">
        </div>
        <button class="submit-btn" onclick="submitPasswordLogin()">Sign In</button>
      </div>

      <!-- OTP Login Form -->
      <div id="otp-login-box" class="hidden">
        <div class="form-group">
          <label>Registered Phone Number (10 digits)</label>
          <input type="text" id="otp-phone" placeholder="enter a valid phone number" autocomplete="off" value="">
        </div>
        <button class="submit-btn" onclick="requestOTP()">Send Verification OTP</button>

        <div id="verify-otp-subbox" class="hidden" style="margin-top: 1.5rem;">
          <div class="form-group">
            <label>Enter 6-digit Code (Output to Terminal)</label>
            <input type="text" id="otp-code" placeholder="123456" autocomplete="off" value="">
          </div>
          <button class="submit-btn" style="background:#059669;" onclick="verifyOTP()">Verify Code & Enter</button>
        </div>
      </div>

      <!-- Bottom Center Registration Trigger -->
      <div class="bottom-switch-link">
        <span>Not yet registered? </span><a onclick="showRegistrationView()">Click here to register</a>
      </div>
    </div>

    <!-- Student Registration Container -->
    <div id="register-container" class="hidden">
      <h3 style="margin-top: 0; margin-bottom: 1.25rem;">Student Registration</h3>
      <div class="form-group">
        <label>Full Name</label>
        <input type="text" id="reg-name" placeholder="John Doe" autocomplete="off">
      </div>
      <div class="form-group">
        <label>Email Address</label>
        <input type="email" id="reg-email" placeholder="student@example.com" autocomplete="off">
      </div>
      <div class="form-group">
        <label>Mobile Number (10 digits)</label>
        <input type="text" id="reg-phone" placeholder="enter a valid phone number" autocomplete="off">
      </div>
      <div class="form-group">
        <label>Create Password</label>
        <input type="password" id="reg-password" placeholder="••••••••" autocomplete="new-password">
      </div>
      <button class="submit-btn" style="background:#059669;" onclick="submitRegistration()">Create Student Account</button>

      <!-- Bottom Center Login Trigger -->
      <div class="bottom-switch-link">
        <span>Already have an account? </span><a onclick="showLoginView()">Click here to sign in</a>
      </div>
    </div>

  </div>

  <!-- Student Workspace Layout -->
  <div id="student-view" class="student-layout hidden">
    <!-- Left Sidebar: Lessons -->
    <div class="lessons-sidebar">
      <div class="sidebar-header" id="course-title-header">Course Modules</div>
      <div id="lessons-list">Loading modules...</div>
    </div>

    <!-- Main Workspace -->
    <div class="video-workspace">
      <div id="video-content-pane">
        <div style="display:flex; justify-content: space-between; align-items: baseline; margin-bottom: 0.75rem;">
          <h3 id="current-lesson-title" style="margin: 0;">Select a Lesson</h3>
          <span id="video-status-badge" class="badge badge-neutral">Status: Unwatched</span>
        </div>
        
        <div id="media-player-wrapper" class="video-container">
          <!-- Dynamic Video/Embed inserted here -->
        </div>

        <div class="assignment-card">
          <h4 style="margin-top: 0; margin-bottom: 0.5rem;">Lesson Assignment</h4>
          <p id="assignment-prompt-text" style="color: #475569; margin: 0.5rem 0;"></p>
          
          <div id="assignment-link-container"></div>

          <div style="display: flex; align-items: center; justify-content: space-between; margin-top: 1rem;">
            <button id="submit-assignment-btn" class="submit-btn" style="width: auto; padding: 0.6rem 1.25rem; margin: 0;" onclick="submitAssignment()">
              Mark Assignment Completed
            </button>
            <span id="assignment-status-text" class="badge badge-neutral">Status: Not Submitted</span>
          </div>
        </div>
      </div>

      <div id="student-no-courses" class="hidden">
        <h3>No Enrolled Courses Found</h3>
        <p>Your account is active, but you have not yet been assigned to an active curriculum. Please contact your administrator.</p>
      </div>

      <div id="student-pending-alert" class="hidden">
        <h3>Account Clearance Pending</h3>
        <p style="color: #b45309; line-height: 1.5;">Your registration has been submitted and is awaiting administrative clearance. Once an administrator approves your account in the admin panel, your curriculum will unlock.</p>
      </div>
    </div>
  </div>

  <!-- Admin View -->
  <div id="admin-view" class="card hidden">
    <h3>Admin Management Portal</h3>
    <div id="admin-metrics" style="display:flex; gap:1rem; margin-bottom: 1.5rem;"></div>
    <h4>Pending Candidate Access Approvals</h4>
    <div id="admin-pending-table-container">Loading approvals...</div>
  </div>

  <!-- Trainer View -->
  <div id="trainer-view" class="card hidden">
    <h3>Trainer Dashboard</h3>
    <div id="trainer-courses">Loading...</div>
  </div>
</div>

<script>
  let activeStudentData = null;
  let activeEnrollmentId = null;
  let activeModuleId = null;

  function clearAuthFields() {
    document.getElementById('login-email').value = '';
    document.getElementById('login-password').value = '';
    document.getElementById('otp-phone').value = '';
    document.getElementById('otp-code').value = '';
    const verifyBox = document.getElementById('verify-otp-subbox');
    if (verifyBox) verifyBox.classList.add('hidden');
  }

  function switchLoginRole(role) {
    document.querySelectorAll('.role-tabs .tab-btn').forEach(btn => btn.classList.remove('active'));
    const tabElement = document.getElementById(`tab-${role === 'student-otp' ? 'otp' : role}`);
    if (tabElement) tabElement.classList.add('active');

    clearAuthFields();

    const passBox = document.getElementById('password-login-box');
    const otpBox = document.getElementById('otp-login-box');

    if (role === 'student-otp') {
      passBox.classList.add('hidden');
      otpBox.classList.remove('hidden');
    } else {
      passBox.classList.remove('hidden');
      otpBox.classList.add('hidden');
    }
  }

  function showRegistrationView() {
    document.getElementById('login-container').classList.add('hidden');
    document.getElementById('register-container').classList.remove('hidden');
    document.getElementById('reg-name').value = '';
    document.getElementById('reg-email').value = '';
    document.getElementById('reg-phone').value = '';
    document.getElementById('reg-password').value = '';
  }

  function showLoginView() {
    document.getElementById('register-container').classList.add('hidden');
    document.getElementById('login-container').classList.remove('hidden');
    switchLoginRole('student');
  }

  window.addEventListener('DOMContentLoaded', () => {
    clearAuthFields();
    switchLoginRole('admin');
  });

  async function submitPasswordLogin() {
    const email = document.getElementById('login-email').value.trim();
    const password = document.getElementById('login-password').value;

    if (!email || !password) {
      alert("Please enter both email and password.");
      return;
    }

    try {
      const res = await fetch('/auth/login-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password })
      });

      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || "Authentication failed");
        return;
      }
      handleLoginSuccess(data.access_token, data.role);
    } catch (err) {
      alert("Connection error occurred.");
    }
  }

  async function submitRegistration() {
    const name = document.getElementById('reg-name').value.trim();
    const email = document.getElementById('reg-email').value.trim();
    const phone_number = document.getElementById('reg-phone').value.trim();
    const password = document.getElementById('reg-password').value;

    if (!name || !email || !phone_number || !password) {
      alert("Please fill in all fields.");
      return;
    }

    try {
      const res = await fetch('/auth/register-student', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, email, phone_number, password })
      });

      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || "Registration failed");
        return;
      }

      alert("Registration submitted! Your account is now pending admin clearance.");
      handleLoginSuccess(data.access_token, data.role);
    } catch (err) {
      alert("Connection error during registration.");
    }
  }

  async function requestOTP() {
    const phone = document.getElementById('otp-phone').value.trim();
    if (!phone) {
      alert("Please enter a phone number.");
      return;
    }
    try {
      const res = await fetch('/auth/request-otp', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone_number: phone })
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || "Error requesting OTP");
        return;
      }
      alert(data.message);
      document.getElementById('verify-otp-subbox').classList.remove('hidden');
    } catch (err) {
      alert("Network request error.");
    }
  }

  async function verifyOTP() {
    const phone = document.getElementById('otp-phone').value.trim();
    const code = document.getElementById('otp-code').value.trim();
    if (!phone || !code) {
      alert("Please provide both phone number and OTP code.");
      return;
    }
    try {
      const res = await fetch('/auth/verify-otp', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone_number: phone, otp_code: code })
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || "Verification failed");
        return;
      }
      handleLoginSuccess(data.access_token, data.role);
    } catch (err) {
      alert("Network request error.");
    }
  }

  function handleLoginSuccess(token, role) {
    localStorage.setItem('jwt_token', token);
    localStorage.setItem('user_role', role);

    document.getElementById('auth-section').classList.add('hidden');
    document.getElementById('logout-btn').classList.remove('hidden');

    if (role === 'student') {
      document.getElementById('profile-btn').classList.remove('hidden');
      renderStudentDashboard();
    } else if (role === 'admin') {
      renderAdminDashboard();
    } else {
      document.getElementById('trainer-view').classList.remove('hidden');
    }
  }

  function handleLogout() {
    localStorage.clear();
    sessionStorage.clear();
    location.reload();
  }

  async function openProfileModal() {
    const token = localStorage.getItem('jwt_token');
    document.getElementById('profile-modal').classList.remove('hidden');

    try {
      const res = await fetch('/user/profile-details', {
        headers: { 'Authorization': 'Bearer ' + token }
      });
      const profile = await res.json();

      document.getElementById('profile-details-content').innerHTML = `
        <div style="line-height: 1.8;">
          <p><strong>Name:</strong> ${profile.name}</p>
          <p><strong>Email:</strong> ${profile.email}</p>
          <p><strong>Phone Number:</strong> ${profile.phone_number || 'N/A'}</p>
          <p><strong>Role:</strong> <span class="badge badge-neutral">${profile.role.toUpperCase()}</span></p>
          <p><strong>Clearance Status:</strong> ${profile.is_access_granted ? '<span class="badge badge-success">Approved</span>' : '<span class="badge badge-warning">Pending Clearance</span>'}</p>
          <p><strong>Enrolled Courses:</strong> ${profile.courses && profile.courses.length ? profile.courses.join(', ') : 'None'}</p>
        </div>
      `;
    } catch (err) {
      document.getElementById('profile-details-content').innerText = "Failed to load profile details.";
    }
  }

  function closeProfileModal() {
    document.getElementById('profile-modal').classList.add('hidden');
  }

  /* Student Dashboard Engine */
  async function renderStudentDashboard() {
    document.getElementById('student-view').classList.remove('hidden');
    const token = localStorage.getItem('jwt_token');

    try {
      const res = await fetch('/student/dashboard', {
        headers: { 'Authorization': 'Bearer ' + token }
      });
      const data = await res.json();

      if (data.alert) {
        document.getElementById('video-content-pane').classList.add('hidden');
        document.getElementById('student-pending-alert').classList.remove('hidden');
        return;
      }

      if (!data.courses || data.courses.length === 0) {
        document.getElementById('video-content-pane').classList.add('hidden');
        document.getElementById('student-no-courses').classList.remove('hidden');
        return;
      }

      const activeCourse = data.courses[0];
      activeEnrollmentId = activeCourse.enrollment_id;
      activeStudentData = activeCourse;

      document.getElementById('course-title-header').innerText = activeCourse.course_title;

      const listContainer = document.getElementById('lessons-list');
      listContainer.innerHTML = '';

      activeCourse.modules.forEach((mod, idx) => {
        const item = document.createElement('div');
        item.className = 'lesson-item' + (!mod.is_unlocked ? ' locked' : '') + (idx === 0 ? ' active' : '');
        item.id = `mod-tab-${mod.module_id}`;

        let statusBadge = mod.assignment_status === 'approved' 
          ? '<span class="badge badge-success">Passed</span>'
          : mod.assignment_status === 'submitted'
          ? '<span class="badge badge-warning">Submitted</span>'
          : mod.is_video_completed 
          ? '<span class="badge badge-warning">Video Done</span>'
          : '<span class="badge badge-neutral">Not Started</span>';

        item.innerHTML = `
          <div style="font-weight: 600; font-size: 0.95rem;">Lesson ${mod.order_index}: ${mod.title}</div>
          <div style="display:flex; justify-content: space-between; align-items:center; margin-top: 0.35rem;">
            ${statusBadge}
            <small style="color: ${mod.is_unlocked ? '#166534' : '#b45309'};">${mod.is_unlocked ? 'Available' : 'Locked'}</small>
          </div>
          ${!mod.is_unlocked && mod.lock_reason ? `<div style="font-size:0.75rem; color:#b45309; margin-top:0.25rem;">${mod.lock_reason}</div>` : ''}
        `;

        if (mod.is_unlocked) {
          item.onclick = () => selectLesson(mod.module_id);
        }

        listContainer.appendChild(item);
      });

      if (activeCourse.modules.length > 0) {
        selectLesson(activeCourse.modules[0].module_id);
      }
    } catch (err) {
      alert("Error loading student curriculum.");
    }
  }

  function selectLesson(moduleId) {
    activeModuleId = moduleId;
    const mod = activeStudentData.modules.find(m => m.module_id === moduleId);
    if (!mod) return;

    document.querySelectorAll('.lesson-item').forEach(el => el.classList.remove('active'));
    const currentTab = document.getElementById(`mod-tab-${moduleId}`);
    if (currentTab) currentTab.classList.add('active');

    document.getElementById('current-lesson-title').innerText = `Lesson ${mod.order_index}: ${mod.title}`;

    const playerWrapper = document.getElementById('media-player-wrapper');
    if (mod.video_url.includes('youtube.com') || mod.video_url.includes('youtu.be')) {
      let ytId = mod.video_url.includes('v=') ? mod.video_url.split('v=')[1].split('&')[0] : mod.video_url.split('/').pop();
      playerWrapper.innerHTML = `<iframe id="yt-player" src="https://www.youtube.com/embed/${ytId}?enablejsapi=1" allowfullscreen></iframe>`;
    } else {
      playerWrapper.innerHTML = `<video id="html5-video" controls src="${mod.video_url}"></video>`;
      const videoEl = document.getElementById('html5-video');
      videoEl.onended = () => autoMarkVideoCompleted();
    }

    const videoBadge = document.getElementById('video-status-badge');
    if (mod.is_video_completed) {
      videoBadge.className = 'badge badge-success';
      videoBadge.innerText = 'Status: Video Completed';
    } else {
      videoBadge.className = 'badge badge-neutral';
      videoBadge.innerText = 'Status: Unwatched';
    }

    document.getElementById('assignment-prompt-text').innerText = mod.assignment_prompt || 'Complete the exercise provided in the assignment link below.';
    const linkContainer = document.getElementById('assignment-link-container');
    if (mod.assignment_link) {
      linkContainer.innerHTML = `
        <a href="${mod.assignment_link}" target="_blank" class="assignment-link-btn">
          <span>&#128279; Open Trainer Assignment Link &rarr;</span>
        </a>
      `;
    } else {
      linkContainer.innerHTML = '<p style="color:#64748b; font-style:italic;">No external link attached for this lesson.</p>';
    }

    const assignBadge = document.getElementById('assignment-status-text');
    const submitBtn = document.getElementById('submit-assignment-btn');

    if (mod.assignment_status === 'approved') {
      assignBadge.className = 'badge badge-success';
      assignBadge.innerText = 'Status: Approved by Trainer';
      submitBtn.classList.add('hidden');
    } else if (mod.assignment_status === 'submitted') {
      assignBadge.className = 'badge badge-warning';
      assignBadge.innerText = 'Status: Submitted (Awaiting Trainer Evaluation)';
      submitBtn.classList.add('hidden');
    } else {
      assignBadge.className = 'badge badge-neutral';
      assignBadge.innerText = 'Status: Pending Submission';
      submitBtn.classList.remove('hidden');
    }
  }

  async function autoMarkVideoCompleted() {
    if (!activeEnrollmentId || !activeModuleId) return;
    const token = localStorage.getItem('jwt_token');

    try {
      await fetch(`/student/complete-video/${activeEnrollmentId}/${activeModuleId}`, {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + token }
      });
      renderStudentDashboard();
    } catch (err) {
      console.error("Auto video completion failed:", err);
    }
  }

  async function submitAssignment() {
    if (!activeEnrollmentId || !activeModuleId) return;
    const token = localStorage.getItem('jwt_token');

    await fetch(`/student/complete-video/${activeEnrollmentId}/${activeModuleId}`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token }
    });

    const formData = new FormData();
    formData.append('enrollment_id', activeEnrollmentId);
    formData.append('module_id', activeModuleId);
    formData.append('submission_text', 'Assignment marked completed via trainer external link.');

    try {
      const res = await fetch('/student/submit-assignment', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + token },
        body: formData
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || "Submission failed");
        return;
      }
      alert("Assignment successfully submitted for review!");
      renderStudentDashboard();
    } catch (err) {
      alert("Error submitting assignment.");
    }
  }

  /* Admin Dashboard Render */
  async function renderAdminDashboard() {
    document.getElementById('admin-view').classList.remove('hidden');
    const token = localStorage.getItem('jwt_token');

    const res = await fetch('/admin/dashboard', {
      headers: { 'Authorization': 'Bearer ' + token }
    });
    const metrics = await res.json();

    document.getElementById('admin-metrics').innerHTML = `
      <div style="background:#eff6ff; padding:1rem; border-radius:6px; flex:1;">
        Trainers: <strong>${metrics.total_trainers}</strong>
      </div>
      <div style="background:#eff6ff; padding:1rem; border-radius:6px; flex:1;">
        Students: <strong>${metrics.total_students}</strong>
      </div>
      <div style="background:#fef3c7; padding:1rem; border-radius:6px; flex:1;">
        Pending Clearance: <strong>${metrics.pending_student_access_approvals}</strong>
      </div>
    `;

    const pendingRes = await fetch('/admin/pending-access-list', {
      headers: { 'Authorization': 'Bearer ' + token }
    });
    const pending = await pendingRes.json();

    if (pending.length === 0) {
      document.getElementById('admin-pending-table-container').innerHTML = "<p>No pending approvals.</p>";
      return;
    }

    let rows = pending.map(s => `
      <tr>
        <td>${s.name}</td>
        <td>${s.email}</td>
        <td>${s.phone_number}</td>
        <td><button onclick="grantAccess('${s.id}')">Grant Access</button></td>
      </tr>
    `).join('');

    document.getElementById('admin-pending-table-container').innerHTML = `
      <table style="width:100%; border-collapse:collapse; margin-top:1rem;">
        <thead><tr><th>Name</th><th>Email</th><th>Phone</th><th>Action</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  async function grantAccess(id) {
    const token = localStorage.getItem('jwt_token');
    await fetch('/admin/grant-access/' + id, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token }
    });
    renderAdminDashboard();
  }
</script>

</body>
</html>"""


def seed_role_account(db, email, name, role, password, specialization=None):
    import uuid

    import auth
    import models

    if len(password.encode("utf-8")) > 1024:
        raise ValueError("Password must be 1024 bytes or fewer.")

    user = db.query(models.User).filter_by(email=email).first()
    if user and user.role != role:
        raise ValueError(f"{email} already exists with role '{user.role}'.")

    if not user:
        user = models.User(
            id=str(uuid.uuid4()),
            name=name,
            email=email,
            role=role,
            is_access_granted=True,
        )
        db.add(user)

    user.name = name
    user.specialization = specialization
    user.hashed_password = auth.get_password_hash(password)
    user.is_access_granted = True
    db.commit()
    return user


def load_seed_accounts(path=None):
    import json
    from pathlib import Path

    accounts_path = Path(path) if path else Path(__file__).with_name("seed_accounts.json")
    with accounts_path.open(encoding="utf-8") as accounts_file:
        accounts = json.load(accounts_file)
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("Seed accounts must be a non-empty JSON list.")

    expected_roles = {"admin", "trainer"}
    seen_roles = set()
    seen_emails = set()
    for account in accounts:
        if not isinstance(account, dict):
            raise ValueError("Each seed account must be a JSON object.")
        email = account.get("email")
        role = account.get("role")
        password = account.get("password")
        name = account.get("name")
        specialization = account.get("specialization")
        if role not in expected_roles:
            raise ValueError("Seed account role must be 'admin' or 'trainer'.")
        if not all(isinstance(value, str) and value.strip() for value in (email, name, password)):
            raise ValueError("Each seed account requires a non-empty email, name, and password.")
        if specialization is not None and (
            not isinstance(specialization, str)
            or len(specialization.strip()) > 255
        ):
            raise ValueError("Account specialization must be a string of 255 characters or fewer.")
        if len(password.encode("utf-8")) > 1024:
            raise ValueError(f"Password for {email} must be 1024 bytes or fewer.")
        if role in seen_roles:
            raise ValueError(f"Only one seed account per role is supported: {role}.")
        if email.casefold() in seen_emails:
            raise ValueError(f"Duplicate seed account email: {email}.")
        seen_roles.add(role)
        seen_emails.add(email.casefold())

    if seen_roles != expected_roles:
        raise ValueError("Seed accounts must include one admin and one trainer.")
    return accounts


def seed_admin_and_trainer():
    import models
    from database import SessionLocal, engine
    from sqlalchemy import inspect, text

    models.Base.metadata.create_all(bind=engine)
    if "specialization" not in {
        column["name"] for column in inspect(engine).get_columns("users")
    }:
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE users ADD COLUMN specialization VARCHAR(255)"
            ))
    if "course_subject" not in {
        column["name"] for column in inspect(engine).get_columns("users")
    }:
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE users ADD COLUMN course_subject VARCHAR(100)"
            ))
    accounts = load_seed_accounts()
    db = SessionLocal()
    try:
        for account in accounts:
            seed_role_account(
                db,
                account["email"],
                account["name"],
                account["role"],
                account["password"],
                account.get("specialization"),
            )
            print(f"Provisioned {account['role']} account: {account['email']}")
    finally:
        db.close()


if __name__ == "__main__":
    seed_admin_and_trainer()