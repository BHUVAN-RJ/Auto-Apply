# job-autopilot

Semi-autonomous job application pipeline. See [PLAN.md](PLAN.md) for the design.

## Setup

```sh
brew install --cask basictex        # needs sudo
uv venv && uv pip install -e .
cp .env.example .env                # add your OpenRouter key
```

Load the capture extension: Chrome → `chrome://extensions` → Developer mode →
Load unpacked → select `capture/`.

## Run

```sh
.venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 8787
```

Open http://127.0.0.1:8787 for the queue and review UI. Right-click any job
posting in Chrome and choose "Add this job to autopilot" to queue it.

## Guarantee

The agent never submits an application. It fills the form, screenshots it, and
stops. Submission is always a human click.
