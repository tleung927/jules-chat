# jchat CLI

`jchat` is a Python command-line interface for interacting with Google Jules sessions. It allows you to create new coding sessions, resume existing ones, approve plans, view diffs, and chat directly with the Jules agent from your terminal. Replies stream in seamlessly while keeping your prompt fully usable, and it handles non-ASCII characters gracefully.

## Installation

`jchat` requires Python 3. It depends on `requests` for interacting with the Jules API and `python-dotenv` for loading configuration.

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/tleung927/jules-chat.git
    cd jules-chat
    ```

2.  **Install the dependencies:**
    Use `pip` to install the requirements:
    ```bash
    pip install -r requirements.txt
    ```
    This installs `requests` and `python-dotenv`.

## Configuration

`jchat` uses an environment variable for authentication. Create a `.env` file in the same directory as `jchat.py` (or set it in your environment) and add your Jules API key.

1.  **Create `.env`:**
    ```
    JULES_API_KEY="your_api_key_here"
    ```

## Usage

You can use `jchat` to start new tasks, list your existing sessions, or resume a specific session.

### Command-Line Flags

*   **`-h`, `--help`**: Show the help message and exit.
*   **`-n`, `--new [PROMPT]`**: Start a new session with this task. If you omit the prompt string, it will interactively ask you for the task. If you don't specify a repository, it presents a numbered picker of connected repos and then a branch picker (defaulting to the repo's default branch).
*   **`-r`, `--resume [RESUME]`**: Resume an existing session. You can pass the list index (e.g., `2` for the second session in the list) or the full session ID. If you omit the value, it lists recent sessions instead.
*   **`-l`, `--list`**: List your recent Jules sessions and exit.
*   **`-S`, `--sources`**: List the repositories Jules is connected to and can work on (along with their default branches), then exit.
*   **`--repo OWNER/REPO`**: Specify the repository for a new session, skipping the interactive picker. It accepts either `owner/repo` or just the bare repo name if it is unambiguous.
*   **`--branch NAME`**: Specify the starting branch for a new session (defaults to the repo's default branch).
*   **`--title TITLE`**: Set a custom session title for a new session (defaults to the first line of your task prompt).
*   **`--auto-pr`**: Configure Jules to automatically open a pull request when the change is ready (`automationMode=AUTO_CREATE_PR`).
*   **`--no-plan-approval`**: Allow Jules to start working immediately without waiting for you to approve its plan. (By default, plan approval is ON for new sessions).

**Note on non-interactive environments:** If you are running `jchat` outside a terminal (e.g., piped or scripted), the interactive pickers cannot run. In these cases, you *must* provide both `--repo` and a task string (e.g., `-n "my task" --repo my/repo`).

### Examples

Start a new session interactively:
```bash
jchat -n "add unit tests for the auth module"
```

Start a new session on a specific repo and branch, without manual plan approval:
```bash
jchat -n "fix the flaky test" --repo me/app --branch dev --no-plan-approval
```

List recent sessions:
```bash
jchat -l
```

Resume session #2 from the list:
```bash
jchat -r 2
```

## In-Chat Commands

Once you are in an active session, anything you type is sent to Jules as a message. Replies stream in continuously; you can keep typing while Jules is working.

There are several built-in slash commands to manage the session state and display information:

*   **`/approve`, `/a`**: Approve the pending plan. If a plan is pending, you can also just type `y`.
*   **`/plan`**: Re-print the most recent plan Jules generated.
*   **`/diff`**: Print the most recent change set patch (the code modifications Jules has made).
*   **`/state`**: Show the current session state and the web UI URL.
*   **`/wait`, `/w`**: Block the prompt until Jules stops working. (Note: Jules' replies stream in anyway; this command is just for when you would rather not type over streaming output).
*   **`/refresh`, `/r`**: Fetch and print anything new from the API immediately.
*   **`/verbose`, `/v`**: Toggle full progress details, bash output, and diffs on/off.
*   **`/clear`**: Clear the terminal screen.
*   **`/help`, `/?`**: Show the list of available in-chat commands.
*   **`/exit`, `/quit`**: Leave the chat CLI. The Jules session itself keeps running in the background.
