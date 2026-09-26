#!/usr/bin/env python3
"""
check_balance.py

Diagnostic script that strips quotes/spaces, prints token fingerprints,
and performs a clean handshake with Capital.com DEMO.
"""
import os
import sys
import httpx

env_vars = {}
if not os.path.exists(".env"):
    print("Error: .env file not found!")
    sys.exit(1)

with open(".env", "r") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            # Strip whitespace AND surrounding quotes
            env_vars[key.strip()] = val.strip().strip("'\"")

api_key = env_vars.get("CAPITAL_API_KEY", "")
identifier = env_vars.get("CAPITAL_IDENTIFIER", "")
password = env_vars.get("CAPITAL_PASSWORD", "")

print("--- Credential Diagnostics ---")
print(f"Identifier (Email): {identifier}")
print(f"API Key Length:     {len(api_key)} chars (Starts with: {api_key[:4]}...)")
print(f"Password Length:    {len(password)} chars")
print("------------------------------")

DEMO_URL = "https://demo-api-capital.backend-capital.com"
print("Attempting session handshake with Capital.com DEMO...")

headers = {
    "X-CAP-API-KEY": api_key,
}
payload = {
    "identifier": identifier,
    "password": password,
    "encryptedPassword": False,
}

response = httpx.post(f"{DEMO_URL}/api/v1/session", headers=headers, json=payload)

if response.status_code == 200:
    data = response.json()
    account_info = data.get("accountInfo", {})
    currency = data.get("currencyIsoCode", "USD")
    balance = account_info.get("balance", 0.0)
    available = account_info.get("available", 0.0)

    print("\n Handshake Successful!")
    print(f"Account ID: {data.get('currentAccountId')}")
    print(f"Demo Balance: {balance} {currency}")
    print(f"Available to trade: {available} {currency}")
else:
    print(f"\n Handshake Failed (Status Code: {response.status_code})")
    print(f"Error Details: {response.text}")