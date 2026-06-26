# CONTEXT.md — Project Security Constitution
# ================================================
# PROJECT SECURITY STANDARDS (June 2026 Edition)
# ================================================

1. **API Keys & Credentials**: Never hardcode API keys, tokens, or credentials in any file. All keys must be read from environment variables or secure vault storage.
2. **Input Sanitization**: All user-uploaded filenames and form metadata must be sanitized (e.g. removing path traversal characters, command characters) before use in agent contexts to prevent Prompt Injection attacks.
3. **Dependency Integrity**: No Python packages may be installed or imported without being explicitly listed in requirements.txt (Defense against Slopsquatting).
4. **Spectator Privacy**: Never expose real names, school names, email addresses, or specific seating locations (e.g. Row 14 Seat 12) in synthesized stories or cofounder logs without user permission.
5. **Human-in-the-Loop (HITL) Gate**: Any action sharing match diaries publicly (e.g., generating public sharing links or scanning QR codes for stadium-wide sharing) requires explicit user confirmation. The system must pause and request approval.
6. **Minor/Children Protection**: Media featuring children or minors must be treated with high-security guardrails. Captions must refer to them collectively as "the little ones" or using emojis, and their photos must be masked/omitted from public sharing unless explicit parental/guardian consent is confirmed.

# ================================================
# Antigravity reads this file and enforces these rules in all generated code.
