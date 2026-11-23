---
name: nvidia-openai-platform
description: Publish reviewed NVIDIA skills to OpenAI Platform.
license: Apache-2.0
metadata:
  hermes:
    tags: [nvidia, openai, agent-skills, platform, responses-api]
    category: mlops
---

# NVIDIA Skills → OpenAI Platform

Use this skill when the task is to publish an NVIDIA Agent Skill to the hosted OpenAI Skills API or attach a published skill to a Responses API agent.

## Source and review

1. Search the NVIDIA provider in Hermes Skills Hub. Use the exact identifier returned by the hub; the trusted repository is `NVIDIA/skills` (capitalization matters for provenance).
2. Install the selected skill through Hermes Skills Hub, then inspect its complete `SKILL.md`, linked references, scripts, `skill-card.md`, and `skill.oms.sig` when present. Do not claim that the OMS signature was cryptographically verified unless a supported verifier has actually verified it.
3. Upload only a skill that is relevant to a specific workflow and has been reviewed by the developer. Do not mirror the entire NVIDIA catalog into a user-facing agent. Preserve source attribution and licensing.
4. Do not expose an `OPENAI_API_KEY` in chat, prompts, logs, source control, `.env` committed to Git, or command-line arguments. Read it from a protected runtime environment variable or secret manager.

Example catalog discovery:

```bash
hermes skills search nvidia --source nvidia --json --limit 100
```

Install the selected exact identifier through Hermes:

```bash
hermes skills install NVIDIA/skills/skills/<skill-name>
```

Use the exact identifier returned by search; the example path is illustrative.

## Prepare and publish

The included publisher uses Python standard library only. It validates one root `SKILL.md`, checks OpenAI's bundle limits, rejects symlinks and likely secret files, creates a ZIP with one top-level folder, and prints a SHA-256 digest.

First run a local validation (this is the default and makes no network request):

```bash
python3 scripts/publish_to_openai.py --skill-dir /absolute/path/to/installed/skill
```

Publish a new hosted skill only when explicitly requested:

```bash
# OPENAI_API_KEY is injected by the protected runtime secret manager.
python3 scripts/publish_to_openai.py --skill-dir /absolute/path/to/installed/skill --publish
```

To add a new immutable version to an existing hosted skill:

```bash
python3 scripts/publish_to_openai.py --skill-dir /absolute/path/to/installed/skill --skill-id skill_... --publish
```

The publisher never changes an existing skill's default version and never deletes a skill. It waits for the uploaded version to become retrievable before reporting success. Keep the returned `skill_id` and `version` in deployment configuration.

## Publish from GitHub Actions

The repository includes a protected, manual workflow at `.github/workflows/publish-openai-skill.yml`. Configure the GitHub Actions environment named `openai-skills-publish` and add `OPENAI_API_KEY` as an environment secret. Then run **Actions → Publish reviewed skill to OpenAI Platform** on `main`. The default input performs validation only; set `publish=true` to upload. Supply a repository-relative `skill_dir` for the reviewed NVIDIA skill. To create a new version, also provide the existing `skill_id`. The workflow refuses non-main refs and paths that resolve outside the checked-out repository. Keep environment approval rules enabled for production credentials.

## Attach to the Responses API

Attach only reviewed skill IDs and pin an explicit version:

```json
{
  "tools": [
    {
      "type": "shell",
      "environment": {
        "type": "container_auto",
        "skills": [
          {
            "type": "skill_reference",
            "skill_id": "skill_...",
            "version": "1"
          }
        ]
      }
    }
  ]
}
```

For the Agents API, skill directories are placed inside the sandbox and their parent directories are registered through `environment.capability_directories`; that is a different mechanism from Responses API `skill_reference` attachments.

## Safety boundary

Treat imported skills as privileged instructions and supporting code. Keep a developer-controlled allowlist, restrict skills to the workflows they were reviewed for, and require approval before writes or other high-impact actions. The publisher validates packaging; it does not replace source review, NVIDIA signature verification, or an application-specific security review.
