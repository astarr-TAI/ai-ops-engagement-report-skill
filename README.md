# ai-ops-engagement-report-skill

Shared Treasure Work skills for the CS team. Skills update automatically — no action required after initial setup.

## Setup (one time per machine)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/astarr-TAI/ai-ops-engagement-report-skill/main/install.sh) https://github.com/astarr-TAI/ai-ops-engagement-report-skill.git
```

This will:
1. Clone the repo to `~/.treasure-work/ai-ops-engagement-report-skill`
2. Symlink each skill into `~/.treasure-work/.claude/skills/`
3. Install a launchd job that pulls updates every hour

## Skills

| Skill | Description |
|-------|-------------|
| `cs-engagement-report` | CS engagement health report — scores accounts Green/Yellow/Red against tier-based meeting mandates. Trigger: "Americas team rollup", "EMEA engagement", "{name}'s book of business" |
| `auto-pull-zoom-transcript` | Pull a transcript from a Zoom meeting doc URL. Trigger: any `docs.zoom.us/doc/` URL, "pull zoom transcript", "get zoom transcript" |

## Updating a skill

Edit the files in `cs-engagement-report/` and push to main. All users pick up the change within the hour. No action needed on their end.

## Adding a new skill

1. Create a new directory: `mkdir new-skill-name`
2. Add a `SKILL.md` with the standard frontmatter (`name`, `description`)
3. Add any supporting files (`.py`, `.yaml`, etc.)
4. Push to main — existing installs will symlink the new directory automatically on next pull

> Note: existing users only get new skill symlinks if they re-run `install.sh` or manually symlink. The `update.sh` / launchd job only pulls — it does not create new symlinks. For new skills, send the one-liner below to the team:
> ```bash
> ln -s ~/.treasure-work/ai-ops-engagement-report-skill/new-skill-name ~/.treasure-work/.claude/skills/new-skill-name
> ```

## Logs

Auto-pull logs: `~/.treasure-work/logs/ai-ops-engagement-report-skill-update.log`
