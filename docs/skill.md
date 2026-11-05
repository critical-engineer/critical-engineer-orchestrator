# Setting up the CEO skill

The skill (`skills/ceo/SKILL.md`) lets the engineer's coding assistant query CEO and
pass on the engineer's answers. It reaches the box over SSH with a key that can do
nothing else (T13, D13; the reasoning is in `docs/spikes/T13-skill-transport.md`).

## 1. A dedicated key on the engineer's machine

```sh
ssh-keygen -t ed25519 -f ~/.ssh/ceo_skill -C "ceo skill"
```

`~/.ssh/config`:

```
Host ceo-box
  HostName media-box.local
  User <the user CEO runs as>
  IdentityFile ~/.ssh/ceo_skill
  IdentitiesOnly yes
```

## 2. Pin the key on the box

In `~/.ssh/authorized_keys` of the user CEO runs as, one line (wrapped here):

```
command="/path/to/critical-engineer-orchestrator/.venv/bin/python -m ceo.cli
  skill-shell --env-file /path/to/ceo/env skill:dries-laptop",restrict
  ssh-ed25519 AAAA... ceo skill
```

- `restrict` turns off port forwarding, agent forwarding, X11 and the PTY.
- The forced command runs whatever the client asks for through `ceo.cli skill-shell`,
  which only allows `status`, `questions` and `answer`, splits the command without a
  shell, and takes the identity (`skill:dries-laptop`) from this line, never from the
  client.

## 3. Allow that identity to decide gates

In CEO's settings file add the identity from the line above:

```
CEO_ENGINEER_SKILL=skill:dries-laptop
```

Without it the skill can read status and questions but every answer is refused (C10).

## 4. Install the skill

Copy or link `skills/ceo/` into the assistant's skills directory, e.g. for Claude Code
`~/.claude/skills/ceo/`. Other assistants can use the same `SKILL.md` as instructions.

## Check

```sh
ssh ceo-box status
ssh ceo-box rm -rf /       # refused: not allowed
```
