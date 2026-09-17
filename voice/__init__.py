"""Local speech for the interviewer: whisper.cpp in, Piper out.

Both are subprocess binaries, never Python packages, so the server carries
no ML dependency (see PLAN.md, "The one-click thesis"). Model files are
downloaded into the app data directory on first use.
"""
