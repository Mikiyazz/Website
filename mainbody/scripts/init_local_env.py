"""Generate local-only secrets without printing them or overwriting an existing .env."""
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
target = root / ".env"
if target.exists():
    raise SystemExit(".env already exists; left unchanged.")
text = (root / ".env.example").read_text(encoding="utf-8")
text = text.replace("replace-with-a-random-key-of-at-least-50-characters", secrets.token_hex(32))
text = text.replace("replace-with-a-random-password", secrets.token_hex(24))
with target.open("x", encoding="utf-8", newline="\n") as stream:
    stream.write(text)
print("Created .env for local development. Secrets were not printed.")
