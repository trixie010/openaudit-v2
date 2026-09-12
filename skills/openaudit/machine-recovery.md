# Machine Recovery — restoring the openaudit workstation

For the "laptop died" scenario. Order matters.

## 1. Base tools
- git, python3, node/npm, uv, forge (foundryup), tmux
- gh CLI (push + repo creation): download the release tarball, no sudo needed:
      curl -sL https://github.com/cli/cli/releases/latest/download/gh_linux_amd64.tar.gz | tar -xz -C /tmp
      cp /tmp/gh_*/bin/gh ~/.local/bin/ && gh auth login --web
- vigilo_poc + vigilo_report skills SHIP INSIDE this repo (skills/vigilo_poc, skills/vigilo_report)
  - nothing to restore; opencode picks them up from the repo's skills/ path automatically

## 2. This repo (private clone)
    git clone <your-private-repo-url> ~/openaudit && cd ~/openaudit
    uv sync                      # slither/semgrep venv
    git submodule update --init --recursive    # all deps incl. chainscope + recon-magic

## 3. Re-create what git CANNOT carry (all gitignored or outside repo)
- `env.sh` at the repo root (never pushed) - create from this template:
      # env.sh
      export JSON_RPC_ETHEREUM="https://<your-rpc-provider>/<key>"
      export ETHERSCAN_API_KEY="<your-etherscan-key>"
  then `source env.sh`
- `.claude/skills` symlink:  `mkdir -p .claude && ln -s ../skills .claude/skills`
- medusa + echidna binaries (release tarballs, ~10-60MB):
      curl -sL https://github.com/crytic/medusa/releases/download/v1.5.1/medusa-linux-x64.tar.gz | tar -xz -C ~/.local/bin
      curl -sL https://github.com/crytic/echidna/releases/download/v2.3.3/echidna-2.3.3-x86_64-linux.tar.gz | tar -xz -C ~/.local/bin
- SolidityGuard local clone (proprietary, Step 6.5):
  `git clone --depth 1 https://github.com/alt-research2/SolidityGuard ~/tools/SolidityGuard`
- ChainScope venv: `cd deps/chainscope && uv venv .venv && uv pip install -r requirements.txt
  -p .venv/bin/python && uv pip install -p .venv/bin/python "mcp[cli]<2"` (mcp<2 pin is REQUIRED)
- runes (optional, Category C violation conversion): github.com/Enigma-Dark/runes
- git identity (if anonymity wanted): `git config user.name X; git config user.email Y`

## 4. fuzz-lab (public repo, separate)
    git clone <fuzz-lab-url>   # corpus + harnesses return via plain clone

## 5. Verify
    forge --version && medusa --version && echidna --version
    deps/chainscope/.venv/bin/python cs_profile.py --help
    cd ~/openaudit && uv run slither --version
