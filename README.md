# reaper-bot — Discord setup

This stage gets the bot connected to a test server and prepares the project for
GitHub. The activity rule checker is paused. This starter logs in, prints the
connected server names, and stays online until you stop it.

It does not scan messages, send messages, manage members, or create a database.

## Files

| File | Purpose | Commit to GitHub? |
| --- | --- | --- |
| `bot.py` | Small Discord connection test | Yes |
| `requirements.txt` | The two Python dependencies | Yes |
| `.gitignore` | Excludes credentials and runtime data | Yes |
| `.env.example` | Empty token template | Yes |
| `README.md` | These setup instructions | Yes |
| `.env` | Your real local token; create it yourself | No |
| Future database and runtime data | Keep under `data/` | No |