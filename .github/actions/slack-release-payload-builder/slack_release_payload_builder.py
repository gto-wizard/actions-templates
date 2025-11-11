import json
import os
import re
import sys


def extract_prs(release_body, repo):
    pr_lines = [line.strip() for line in release_body.splitlines() if line.strip().startswith("- ")]
    pr_elements = []

    for line in pr_lines:
        print(line)  # noqa: T201
        match = re.match(r"- (.*)\s+\(#(\d+)\)", line)
        if match:
            message, pr_number = match.groups()
            pr_elements.append(
                {
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "text", "text": f"{message} "},
                        {
                            "type": "link",
                            "url": f"https://github.com/{repo}/pull/{pr_number}",
                            "text": f"(#{pr_number})",
                        },
                    ],
                }
            )

    return pr_elements


def build_payload(inputs, pr_elements):
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Release started: [{inputs['app']}-{inputs['environment']}] :clapper:",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {   "type": "mrkdwn",
                    "text": f"• *Version*: *<{inputs['release_url']}|{inputs['release_name']}>*"},
                {
                    "type": "mrkdwn",
                    "text": f"• *ArgoCD*: <{inputs['argocd_url']}/applications/argocd/{inputs['app']}-{inputs['environment']}|link>",
                },
            ],
        },
    ]

    if pr_elements:
        blocks.append(
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": "• Changes", "style": {"bold": True}}],
                    },
                    {"type": "rich_text_list", "style": "bullet", "indent": 1, "border": 0, "elements": pr_elements},
                ],
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "image",
                    "image_url": "https://image.freepik.com/free-photo/red-drawing-pin_1156-445.jpg",
                    "alt_text": "images",
                },
                {"type": "mrkdwn", "text": f"Initiated by *{inputs['release_actor']}*"},
            ],
        }
    )

    return {
        "channel": inputs["channel"],
        "text": f"Release started: [{inputs['app']}-{inputs['environment']} - {inputs['release_name']}] :clapper:",
        "blocks": blocks,
    }


def main():
    try:
        inputs = {
            "app": os.environ["INPUT_APP"],
            "environment": os.environ["INPUT_ENVIRONMENT"],
            "release_url": os.environ["INPUT_RELEASE_URL"],
            "release_name": os.environ["INPUT_RELEASE_NAME"],
            "release_body": os.environ.get("INPUT_RELEASE_BODY", ""),
            "release_actor": os.environ["INPUT_RELEASE_ACTOR"],
            "github_repo": os.environ["INPUT_GITHUB_REPO"],
            "channel": os.environ["INPUT_CHANNEL"],
            "argocd_url": os.environ["INPUT_ARGOCD_URL"],
        }

        pr_elements = extract_prs(inputs["release_body"], inputs["github_repo"]) if inputs["release_body"] else []
        payload = build_payload(inputs, pr_elements)

    except Exception as e:
        print(f"[WARN] Error generating detailed payload: {e}", file=sys.stderr)  # noqa: T201
        payload = {
            "channel": inputs["channel"],
            "text": f"Error generating detailed payload: [{inputs['app']}-{inputs['environment']} - {inputs['release_name']}] :warning:",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": ":warning: *Release notification could not be constructed properly.*",
                    },
                },
                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Error: `{str(e)}`"}]},
            ],
        }

    try:
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            print(f"slack_payload={json.dumps(payload)}", file=f)  # noqa: T201
    except Exception as output_error:
        print(f"[ERROR] Failed to write to GITHUB_OUTPUT: {output_error}", file=sys.stderr)  # noqa: T201
        sys.exit(1)


if __name__ == "__main__":
    main()
