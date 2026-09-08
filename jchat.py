import argparse
import os
import sys
import time
import requests
import json
from dotenv import load_dotenv

def load_api_key():
    load_dotenv()
    api_key = os.environ.get("JULES_API_KEY")
    if not api_key:
        print("Error: JULES_API_KEY not found in .env file or environment variables.")
        sys.exit(1)
    return api_key

def get_headers(api_key):
    return {
        "X-Goog-Api-Key": api_key,
        "Content-Type": "application/json"
    }

BASE_URL = "https://jules.googleapis.com/v1alpha"

# ANSI color codes
COLOR_USER = "\033[92m" # Green
COLOR_JULES = "\033[94m" # Blue
COLOR_RESET = "\033[0m"
COLOR_SYSTEM = "\033[93m" # Yellow

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def fetch_sessions(headers):
    url = f"{BASE_URL}/sessions?pageSize=10"
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print(f"{COLOR_SYSTEM}Failed to fetch sessions. Status: {response.status_code}{COLOR_RESET}")
        print(response.text)
        sys.exit(1)

    data = response.json()
    sessions = data.get("sessions", [])
    if not sessions and isinstance(data, list):
        sessions = data
    return sessions

def print_sessions(sessions):
    if not sessions:
        print(f"{COLOR_SYSTEM}No active sessions found.{COLOR_RESET}")
        return

    print(f"{COLOR_SYSTEM}Active Sessions:{COLOR_RESET}")
    for idx, session in enumerate(sessions, start=1):
        session_id = session.get("name", session.get("id", "Unknown ID"))
        title = session.get("title", session.get("description", "No Title"))
        print(f"[{idx}] {session_id} - {title}")

def extract_text_from_activity(activity):
    messages = []

    # 1. userPrompt / agentResponse
    if "userPrompt" in activity:
        val = activity["userPrompt"]
        text = val if isinstance(val, str) else val.get("text", val.get("content", ""))
        if text: messages.append(("User", text))

    if "agentResponse" in activity:
        val = activity["agentResponse"]
        text = val if isinstance(val, str) else val.get("text", val.get("content", ""))
        if text: messages.append(("Jules", text))

    if messages: return messages

    # 2. prompt / response
    if "prompt" in activity:
        val = activity["prompt"]
        text = val if isinstance(val, str) else val.get("text", val.get("content", ""))
        if text: messages.append(("User", text))

    if "response" in activity:
        val = activity["response"]
        text = val if isinstance(val, str) else val.get("text", val.get("content", ""))
        if text: messages.append(("Jules", text))

    if messages: return messages

    # 3. role / content or text
    if "role" in activity:
        role_str = str(activity["role"]).lower()
        role = "User" if "user" in role_str else "Jules"

        if "content" in activity:
            messages.append((role, str(activity["content"])))
            return messages
        if "text" in activity:
            messages.append((role, str(activity["text"])))
            return messages

    # 4. text or message (guess role based on type)
    type_str = str(activity.get("type", "")).lower()
    role = "User" if "user" in type_str else "Jules"

    if "text" in activity:
        messages.append((role, str(activity["text"])))
        return messages

    if "message" in activity:
        val = activity["message"]
        if isinstance(val, str):
            messages.append((role, val))
        elif isinstance(val, dict):
            if "content" in val:
                messages.append((role, str(val["content"])))
            elif "text" in val:
                messages.append((role, str(val["text"])))
            else:
                # Just extract any string
                pass
        if messages: return messages

    # 5. Fallback: extract string values recursively to avoid raw JSON
    def extract_strings(d):
        if isinstance(d, dict):
            for k, v in d.items():
                if k.lower() in ['id', 'name', 'type', 'timestamp']: continue # skip metadata
                yield from extract_strings(v)
        elif isinstance(d, list):
            for item in d:
                yield from extract_strings(item)
        elif isinstance(d, str):
            yield d

    strings = list(extract_strings(activity))
    if strings:
        messages.append((role, " ".join(strings)))

    return messages

def fetch_activities(session_name, headers):
    if session_name.startswith("sessions/"):
        url = f"{BASE_URL}/{session_name}/activities?pageSize=30"
    else:
        url = f"{BASE_URL}/sessions/{session_name}/activities?pageSize=30"

    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return []

    data = response.json()
    activities = data.get("activities", [])
    if not activities and isinstance(data, list):
        activities = data

    # Activities might be newest-first. If they have a timestamp, we can sort,
    # but typically it's better to just display them. If we notice they are backwards,
    # we can reverse them. We'll leave as returned for now.
    return activities

def display_chat_history(activities):
    clear_screen()
    print(f"{COLOR_SYSTEM}--- Chat History ---{COLOR_RESET}\n")

    for activity in activities:
        messages = extract_text_from_activity(activity)
        for role, text in messages:
            if role == "User":
                print(f"{COLOR_USER}User: {text}{COLOR_RESET}\n")
            else:
                print(f"{COLOR_JULES}Jules: {text}{COLOR_RESET}\n")

def send_message(session_name, prompt, headers):
    if session_name.startswith("sessions/"):
        url = f"{BASE_URL}/{session_name}:sendMessage"
    else:
        url = f"{BASE_URL}/sessions/{session_name}:sendMessage"

    payload = {"prompt": prompt}
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code != 200:
        print(f"{COLOR_SYSTEM}Failed to send message: {response.text}{COLOR_RESET}")

def chat_loop(session_name, headers):
    while True:
        activities = fetch_activities(session_name, headers)
        display_chat_history(activities)

        try:
            user_input = input(f"{COLOR_USER}> {COLOR_RESET}")
            if user_input.strip().lower() in ['exit', 'quit']:
                print(f"{COLOR_SYSTEM}Exiting chat.{COLOR_RESET}")
                break

            if user_input.strip():
                send_message(session_name, user_input, headers)
                time.sleep(3)
        except (KeyboardInterrupt, EOFError):
            print(f"\n{COLOR_SYSTEM}Exiting chat.{COLOR_RESET}")
            break

def main():
    parser = argparse.ArgumentParser(description="Jules CLI Chat")
    parser.add_argument("-r", "--resume", nargs="?", const="LIST", help="Resume a session (provide index) or list sessions (no index)")

    args = parser.parse_args()

    if args.resume:
        api_key = load_api_key()
        headers = get_headers(api_key)

        sessions = fetch_sessions(headers)

        if args.resume == "LIST":
            print_sessions(sessions)
        else:
            try:
                index = int(args.resume)
                if 1 <= index <= len(sessions):
                    selected_session = sessions[index - 1]
                    session_name = selected_session.get("name", selected_session.get("id"))
                    if session_name:
                        chat_loop(session_name, headers)
                    else:
                        print(f"{COLOR_SYSTEM}Could not determine session ID.{COLOR_RESET}")
                else:
                    print(f"{COLOR_SYSTEM}Invalid session index.{COLOR_RESET}")
            except ValueError:
                print(f"{COLOR_SYSTEM}Please provide a valid numeric index for --resume.{COLOR_RESET}")
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
