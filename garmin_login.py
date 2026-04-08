#!/usr/bin/env python3
"""Login with MFA code passed as argument: python3 garmin_login.py <mfa_code>"""
from dotenv import load_dotenv
load_dotenv()
import os
import sys
import garth
from pathlib import Path

token_dir = Path(__file__).parent / "garmin_data" / ".tokens"
token_dir.mkdir(parents=True, exist_ok=True)

mfa_code = sys.argv[1] if len(sys.argv) > 1 else None

def prompt_mfa():
    if mfa_code:
        return mfa_code
    return input("MFA code: ")

client = garth.Client()
client.sess.headers["User-Agent"] = "GCM-iOS-6.4.0.1"
client.login(os.getenv("GARMIN_EMAIL"), os.getenv("GARMIN_PASSWORD"), prompt_mfa=prompt_mfa)
client.dump(str(token_dir))
print("Tokens saved!")
