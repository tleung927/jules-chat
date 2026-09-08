import argparse
import os
import sys
import time
import requests
import json
import re
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
    # Filter out system "thinking" and "update" messages per user request.
    activity_type = str(activity.get("type", "")).lower()
    if activity_type == "thinking" or activity_type == "update":
        return []

    messages = []

    # 1. userPrompt / agentResponse
    if not messages:
        if "userPrompt" in activity:
            val = activity["userPrompt"]
            text = val if isinstance(val, str) else val.get("text", val.get("content", ""))
            if text: messages.append(("User", text))

        if "agentResponse" in activity:
            val = activity["agentResponse"]
            text = val if isinstance(val, str) else val.get("text", val.get("content", ""))
            if text: messages.append(("Jules", text))

    # 2. prompt / response
    if not messages:
        if "prompt" in activity:
            val = activity["prompt"]
            text = val if isinstance(val, str) else val.get("text", val.get("content", ""))
            if text: messages.append(("User", text))

        if "response" in activity:
            val = activity["response"]
            text = val if isinstance(val, str) else val.get("text", val.get("content", ""))
            if text: messages.append(("Jules", text))

    # 3. role / content or text
    if not messages:
        if "role" in activity:
            role_str = str(activity["role"]).lower()
            role = "User" if "user" in role_str else "Jules"

            if "content" in activity:
                messages.append((role, str(activity["content"])))
            elif "text" in activity:
                messages.append((role, str(activity["text"])))

    # 4. text or message (guess role based on type)
    if not messages:
        type_str = str(activity.get("type", "")).lower()
        # Default to Jules if type isn't clearly user, but check fallback logic carefully
        role = "User" if "user" in type_str else "Jules"

        if "text" in activity:
            messages.append((role, str(activity["text"])))

        elif "message" in activity:
            val = activity["message"]
            if isinstance(val, str):
                messages.append((role, val))
            elif isinstance(val, dict):
                if "role" in val:
                    r = str(val["role"]).lower()
                    role = "User" if "user" in r else "Jules"

                if "content" in val:
                    messages.append((role, str(val["content"])))
                elif "text" in val:
                    messages.append((role, str(val["text"])))

    # 5. Fallback: extract string values recursively to avoid raw JSON
    if not messages:
        def extract_strings(d):
            if isinstance(d, dict):
                for k, v in d.items():
                    if k.lower() in ['id', 'name', 'type', 'timestamp', 'role']: continue
                    yield from extract_strings(v)
            elif isinstance(d, list):
                for item in d:
                    yield from extract_strings(item)
            elif isinstance(d, str):
                yield d

        strings = list(extract_strings(activity))
        if strings:
            content = " ".join(strings)
            # Apply regex to fallback as well
            match = re.search(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s+(user|agent)\s+(.*)', content, re.DOTALL)
            if match:
                found_role = "User" if match.group(1).lower() == "user" else "Jules"
                messages.append((found_role, match.group(2).strip()))
            else:
                messages.append((role, content))

    # Final Cleanup Pass: Check all extracted messages (from any step) for the raw timestamp format
    # and filter out any remaining internal tool traces/scripts.
    cleaned_messages = []
    for r, text in messages:
        match = re.search(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s+(user|agent)\s+(.*)', text, re.DOTALL)
        if match:
            r = "User" if match.group(1).lower() == "user" else "Jules"
            text = match.group(2).strip()

        # Ignore noisy diffs or internal source traces that aren't real conversational messages
        if "diff --git" in text or "sources/github/" in text:
            continue

        cleaned_messages.append((r, text))

    return cleaned_messages

def fetch_activities(session_name, headers):
    if session_name.startswith("sessions/"):
        base_url = f"{BASE_URL}/{session_name}/activities"
    else:
        base_url = f"{BASE_URL}/sessions/{session_name}/activities"

    all_activities = []
    next_page_token = None

    while True:
        params = {"pageSize": 100}
        if next_page_token:
            params["pageToken"] = next_page_token

        response = requests.get(base_url, headers=headers, params=params)
        if response.status_code != 200:
            print(f"{COLOR_SYSTEM}Error fetching activities: {response.text}{COLOR_RESET}")
            break

        data = response.json()

        # If it's just a flat list for some reason
        if isinstance(data, list):
            all_activities.extend(data)
            break

        activities = data.get("activities", [])
        if activities:
            all_activities.extend(activities)

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    # Reversing the list puts the most recent message at the bottom, right above the prompt.
    return list(reversed(all_activities))

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

def count_jules_messages(activities):
    count = 0
    for activity in activities:
        messages = extract_text_from_activity(activity)
        for role, _ in messages:
            if role == "Jules":
                count += 1
    return count

def chat_loop(session_name, headers):
    while True:
        activities = fetch_activities(session_name, headers)
        display_chat_history(activities)

        try:
            user_input = input(f"{COLOR_USER}> {COLOR_RESET}")

            # If user presses enter without typing, just refresh the history
            if not user_input.strip():
                continue

            if user_input.strip().lower() in ['exit', 'quit']:
                print(f"{COLOR_SYSTEM}Exiting chat.{COLOR_RESET}")
                break

            # Print the user's message immediately so it doesn't disappear
            print(f"{COLOR_USER}User: {user_input.strip()}{COLOR_RESET}\n")
            print(f"{COLOR_SYSTEM}Waiting for Jules to respond...{COLOR_RESET}")

            send_message(session_name, user_input, headers)

            # Wait for Jules' response
            max_retries = 15 # 45 seconds total
            original_jules_count = count_jules_messages(activities)

            for _ in range(max_retries):
                time.sleep(3)
                new_activities = fetch_activities(session_name, headers)
                new_jules_count = count_jules_messages(new_activities)
                if new_jules_count > original_jules_count:
                    # New activity from Jules found
                    break

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
