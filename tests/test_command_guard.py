"""
Tokenizer-based Bash deny classifier (command_guard.py) and the audited PreToolUse hook around it.

tests/test_deny_commands.py keeps the original FATAL/SAFE tables (unchanged behaviour). This file
covers what the tokenizer added: the git hook-skip rules, plus-refspec force pushes, git global
options, "data is not a command" vs "interpreter input is a program", the linear-time budget with
its keyword fallback, and the audit trail.  Case strings borrow from the MIT-licensed
everything-claude-code hook tests (block-no-verify / gateguard) and from the measured bypasses in
the 2026-10-09 audit (verify_guard.py / verify_guard2.py).

Test strings are plain data in this file; never paste a denied shape inline into a Bash command of
an agent session (the live guard would block that command too).
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import command_guard as cg
import engine


def rule_of(cmd):
    hit = cg.classify_command(cmd)
    return hit.rule if hit else None


SKIP = "git-skip-hooks"
HOOKS = "git-hookspath"
SCAN = "skip-secret-scan"
PUSH = "git-force-push-protected"
RESET = "git-hard-reset"
RM = "rm-root-home"

# ── DENY: (command, expected rule id) ───────────────────────────────────────────────────────────
DENY = [
    # -- the six measured bypasses (verify_guard.py) --
    ("git commit --no-verify -m x", SKIP),
    ("git commit -anm x", SKIP),
    ("git -c core.hooksPath=/dev/null commit -m x", HOOKS),
    ("SKIP_SECRET_SCAN=1 git commit -m x", SCAN),
    ("git -C /tmp/x reset --hard", RESET),
    ("git push origin +master", PUSH),

    # -- --no-verify and its spellings --
    ("git commit -n -m x", SKIP),
    ('git commit -n -m "msg"', SKIP),
    ('git commit -an -m "msg"', SKIP),
    ('git commit -sn -m "msg"', SKIP),
    ('git commit -vn -m "msg"', SKIP),
    ('git commit -nu -m "msg"', SKIP),             # n comes before the optional-value flag
    ("git commit -m ok; git commit -am x -n", SKIP),
    ('git commit "--no-verify" -m x', SKIP),
    ("git commit '--no-verify' -m x", SKIP),
    ('git commit --no-veri -m "msg"', SKIP),       # git accepts unambiguous prefixes
    ('git commit --no-verif -m "msg"', SKIP),
    ('git push --no-verif origin main', SKIP),
    ("git push --no-verify", SKIP),
    ("git push origin feature --no-verify", SKIP),
    ("git merge --no-verify topic", SKIP),
    ("git rebase --no-verify main", SKIP),
    ("git am --no-verify 0001.patch", SKIP),
    ("git am -n 0001.patch", SKIP),                # `git am -n` IS --no-verify (git am -h)
    ("git cherry-pick --no-verify abc123", SKIP),
    ("git -C /tmp/x commit --no-verify -m x", SKIP),
    ("git --no-pager commit -n -m x", SKIP),
    ("git -c user.name=a commit -n -m x", SKIP),
    ("git commit -m ok && git push --no-verify", SKIP),
    ("git log -n 5 && git commit --no-verify -m msg", SKIP),
    ("git add -A && git commit -m ok && git push --no-verify", SKIP),
    ("git commit -m ok            git push --no-verify", SKIP),   # (one command, still denied)
    ("git commit \\\n--no-verify -m x", SKIP),     # line continuation
    ("git commit --no-verify\ngit status", SKIP),
    ("echo '#'; git push --no-verify", SKIP),
    ('echo "#"; git commit -n -m x', SKIP),
    ('echo "not # a comment" && git commit --no-verify -m x', SKIP),
    ("echo foo#bar; git commit -n -m x", SKIP),
    ("git commit --no-veri # actual option", SKIP),
    ("git push '--no-verify'", SKIP),
    # -- the git executable spelled around the quoting --
    ('"git" commit -n -m x', SKIP),
    ("'git' commit -n -m x", SKIP),
    ("g''it commit -n -m x", SKIP),
    ('g""it commit --no-verify -m x', SKIP),
    ("'g'it commit -n -m x", SKIP),
    ("g'i't commit -n -m x", SKIP),
    ("g\\it commit -n -m x", SKIP),
    ("\\git commit -n -m x", SKIP),
    ("/usr/bin/git commit --no-verify -m x", SKIP),
    ("$'git' commit -n -m x", SKIP),
    ("$'\\x67it' commit -n -m x", SKIP),
    ("git com''mit --no-verify", SKIP),
    ("g\\\nit push --no-verify", SKIP),
    # -- core.hooksPath --
    ("git -c core.hookspath=/dev/null commit -m x", HOOKS),
    ("git -c core.HOOKSPATH=/dev/null commit -m x", HOOKS),
    ('git -c "core.hooksPath=/dev/null" commit -m "msg"', HOOKS),
    ("git -c core.hooksPath=/tmp/no status", HOOKS),            # any git invocation
    ("git -c core.hooksPath= commit -m x", HOOKS),
    ("git --config-env=core.hooksPath=HP commit -m x", HOOKS),
    ("git --config-env core.hooksPath=HP commit -m x", HOOKS),
    ("git -C /tmp/x -c core.hooksPath=/dev/null commit -m x", HOOKS),
    ("'git' -c core.hooksPath=/tmp/no commit -m x", HOOKS),
    ("git config core.hooksPath /dev/null", HOOKS),
    ("git config --global core.hooksPath /dev/null", HOOKS),
    ("git config --local core.hooksPath /dev/null", HOOKS),
    ("git config --global --add core.hooksPath /x", HOOKS),
    ("git config core.hookspath ''", HOOKS),
    ("git config set core.hooksPath /x", HOOKS),
    ("git -C /tmp/x config core.HooksPath /x", HOOKS),
    ("GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null git commit -m x", HOOKS),
    ("GIT_CONFIG_PARAMETERS=\"'core.hookspath'='/dev/null'\" git commit -m x", HOOKS),
    # -- SKIP_SECRET_SCAN --
    ("SKIP_SECRET_SCAN=1 git commit -m x", SCAN),
    ("SKIP_SECRET_SCAN=yes git push", SCAN),
    ("export SKIP_SECRET_SCAN=1", SCAN),
    ("export SKIP_SECRET_SCAN=1; git commit -m x", SCAN),
    ("declare -x SKIP_SECRET_SCAN=1", SCAN),
    ("env SKIP_SECRET_SCAN=1 git commit -m x", SCAN),
    ("env -i HOME=/tmp SKIP_SECRET_SCAN=1 git commit -m x", SCAN),
    ("sudo SKIP_SECRET_SCAN=1 git commit -m x", SCAN),
    ("SKIP_SECRET_SCAN=1", SCAN),                                # assignment-only statement
    ("FOO=1 SKIP_SECRET_SCAN=1 git commit -m x", SCAN),
    ('SKIP_SECRET_SCAN="1" git commit -m x', SCAN),
    ("SKIP_SECRET_SCAN=$FLAG git commit -m x", SCAN),            # not provably empty
    ("SKIP_SECRET_SCAN+=1 git commit -m x", SCAN),
    ("echo ok\nSKIP_SECRET_SCAN=1 git commit -m x", SCAN),
    ("bash -c 'SKIP_SECRET_SCAN=1 git commit -m x'", SCAN),
    ("env -S 'SKIP_SECRET_SCAN=1 git commit -m x'", SCAN),
    # -- plus-refspec = force --
    ("git push origin +master", PUSH),
    ("git push origin +main", PUSH),
    ("git push origin +HEAD:main", PUSH),
    ("git push origin +HEAD:master", PUSH),
    ("git push origin +refs/heads/main", PUSH),
    ("git push origin +refs/heads/main:refs/heads/main", PUSH),
    ("git push origin +feature:master", PUSH),
    ("git push --repo origin +master", PUSH),
    ("git push origin feature +main", PUSH),
    ("git push --force-with-lease origin +refs/heads/master:refs/heads/master", PUSH),
    # -- force flags in the spellings the old text regex missed --
    ("git push -fu origin master", PUSH),
    ("git push -uf origin master", PUSH),
    ("git push origin master -f", PUSH),
    ("git push origin HEAD:master --force", PUSH),
    ("git push origin HEAD:refs/heads/main -f", PUSH),
    ("git push --force origin :master", PUSH),
    ("git push --force --force-if-includes origin main", PUSH),
    ("git push --force-with-lease --force origin main", PUSH),
    ("git push --force-with-lease=master:abc123 origin master", PUSH),
    ("git push -o ci.skip --force origin master", PUSH),
    ("git -C /tmp/x push --force origin master", PUSH),
    ("git --git-dir=/x/.git push -f origin main", PUSH),
    ("git -c push.default=current push -f", PUSH),
    ("sudo git push --force origin master", PUSH),
    # -- git global options must not hide the old denials --
    ("git --git-dir=/x/.git reset --hard", RESET),
    ("git --work-tree /x reset --hard", RESET),
    ("git --work-tree=/x --git-dir=/x/.git reset --hard HEAD~2", RESET),
    ("git --no-pager reset --hard", RESET),
    ("git -P reset --hard", RESET),
    ("git --bare reset --hard", RESET),
    ("git -c core.pager=cat reset --hard", RESET),
    ("git -C /a -C b reset --hard", RESET),
    ("git reset HEAD~1 --hard", RESET),
    ("git reset --ha", RESET),
    ("git reset --har HEAD", RESET),
    ("sudo -u deploy git -C /srv reset --hard", RESET),
    # -- interpreters / wrappers: their input IS a program --
    ("bash -c 'rm -rf /'", RM),
    ('bash -c "git reset --hard"', RESET),
    ("sh -c 'git push --force origin master'", PUSH),
    ("zsh -c 'rm -rf ~'", RM),
    ("bash -lc 'rm -rf $HOME'", RM),
    ("bash --noprofile -c 'git commit -n -m x'", SKIP),
    ("bash -O extglob -c 'git commit -n -m x'", SKIP),
    ("bash -o pipefail -c 'git commit -n -m x'", SKIP),
    ("bash -c -- 'git commit -n -m x'", SKIP),
    ("bash +x -c 'git push --no-verify'", SKIP),
    ("bash -c 'echo ok' ; bash -c 'git push --no-verify'", SKIP),
    ("'bash' -c 'git push --no-verify'", SKIP),
    ("eval 'rm -rf /'", RM),
    ("eval rm -rf /", RM),
    ('eval "git commit --no-verify -m x"', SKIP),
    ("eval 'git' 'push' '--no-verify'", SKIP),
    ("sudo rm -rf /", RM),
    ("doas rm -rf /", RM),
    ("sudo -u root git reset --hard", RESET),
    ("sudo -E -u root bash -c 'rm -rf /'", RM),
    ("sudo -s 'git reset --hard'", RESET),
    ("sudo sh -c 'git commit -n -m x'", SKIP),
    ("env X=1 rm -rf /", RM),
    ("env -i PATH=/bin git reset --hard", RESET),
    ("env -u HOME git push --no-verify", SKIP),
    ("env -- git reset --hard", RESET),
    ("env -S 'git reset --hard'", RESET),
    ("xargs rm -rf /", RM),
    ("xargs -n1 git reset --hard", RESET),
    ("xargs -I{} git commit -n -m {}", SKIP),
    ("nohup rm -rf / &", RM),
    ("nohup git push --no-verify", SKIP),
    ("timeout 10 git reset --hard", RESET),
    ("timeout -s KILL 5 rm -rf /", RM),
    ("nice -n 5 rm -rf /", RM),
    ("time git reset --hard", RESET),
    ("command git reset --hard", RESET),
    ("command -p rm -rf /", RM),
    ("exec git reset --hard", RESET),
    ("setsid git push --no-verify", SKIP),
    ("stdbuf -oL git reset --hard", RESET),
    ("find / -exec rm -rf / \\;", RM),
    ("find . -name x -exec git reset --hard {} +", RESET),
    ("su -c 'git reset --hard'", RESET),
    ("su - deploy -c 'rm -rf /'", RM),
    ("ssh host 'git reset --hard'", RESET),
    ('ssh host "rm -rf /"', RM),
    ("ssh -p 2222 -i key user@host rm -rf /", RM),
    ("ssh host 'git push --force origin master'", PUSH),
    ("ssh -t host 'cd /srv && git commit -n -m x'", SKIP),
    ("ssh -o StrictHostKeyChecking=no host 'git -c core.hooksPath=/x commit -m y'", HOOKS),
    ("bash <<EOF\ngit commit -n -m x\nEOF", SKIP),
    ("bash <<'EOF'\ngit commit -n -m x\nEOF", SKIP),
    ("sh -s <<EOF\ngit push --no-verify\nEOF", SKIP),
    ("bash -s <<'EOF'\nrm -rf /\nEOF", RM),
    ("bash <<-EOF\n\tgit push --no-verify\n\tEOF", SKIP),
    ("cat <<EOF | bash\ngit commit -n -m x\nEOF", SKIP),
    ("cat <<'EOF' | sh\ngit reset --hard\nEOF", RESET),
    ("cat <<EOF | sudo bash\ngit commit --no-verify -m x\nEOF", SKIP),
    ("<<EOF bash\ngit commit -n -m x\nEOF", SKIP),
    ("bash <<< 'git commit -n -m x'", SKIP),
    ("sh -s <<< 'git commit --no-verify -m x'", SKIP),
    ("echo 'git commit -n -m x' | bash", SKIP),
    ("printf '%s\\n' 'git commit --no-verify -m x' | sh", SKIP),
    ("echo 'rm -rf /' | sh", RM),
    ("printf 'git reset --hard\\n' | tee /tmp/x | bash", RESET),   # walk back through the pipe
    ("echo 'git reset --hard' | cat | sh", RESET),
    ("cat <<'EOF' | tee /tmp/x | bash\ngit reset --hard\nEOF", RESET),
    # -- heredoc / comment / case boundaries: what comes after them is a command again --
    ("cat <<A <<B\na\nA\nb\nB\ngit reset --hard", RESET),
    ("cat <<-EOF\n\tgit reset --hard\n\tEOF\ngit reset --hard", RESET),
    ("cat <<EOF\r\ngit reset --hard\r\nEOF\r\ngit reset --hard", RESET),
    ("cat <<EOF;git reset --hard\nEOF", RESET),
    ("cat <<EOF > out\nline\nEOF\ngit push --no-verify", SKIP),
    ("bash <<EOF\nbash <<INNER\ngit reset --hard\nINNER\nEOF", RESET),
    ("echo hi #git reset --hard\ngit reset --hard", RESET),
    ("echo $(case $x in a) ls;; b) git reset --hard;; esac)", RESET),
    ("case $x in (a) git reset --hard;; (b) ls;; esac", RESET),
    ("select i in a b; do git reset --hard; done", RESET),
    ("echo x |& git reset --hard", RESET),
    ("time -p git reset --hard", RESET),
    ("echo \"${x:-$(git reset --hard)}\"", RESET),
    ("echo $(( $(git reset --hard) + 1 ))", RESET),
    ("echo $((1<<3)); ls\ngit reset --hard", RESET),              # `<<` in arithmetic is a shift
    ("(( x = 1 << 3 ))\ngit reset --hard", RESET),
    ("for ((i=0;i<3;i++)); do git reset --hard; done", RESET),
    ("echo $((1<<3)) $(git reset --hard)", RESET),
    ("echo $((cmd1); git reset --hard)", RESET),                    # not arithmetic: a subshell
    ("git reset --hard >/dev/null 2>&1", RESET),
    # -- nested constructs --
    ('echo "$(git reset --hard)"', RESET),
    ("echo $(echo $(git push --no-verify))", SKIP),
    ('git commit -m "$(git push --no-verify)"', SKIP),
    ('git commit -m "$(git -c core.hooksPath=/dev/null push)"', HOOKS),
    ('git commit -m "`git push --no-verify`"', SKIP),
    ("git commit --message=\"$(git push --no-veri)\"", SKIP),
    ("echo `git reset --hard`", RESET),
    ('echo "`git commit -n -m x`"', SKIP),
    ('echo "$(printf \')\'; git commit -n -m x)"', SKIP),
    ('echo "$(case x in x) :;; esac; git commit -n -m x)"', SKIP),
    ("cat <(git push --no-verify)", SKIP),
    ("cat <<EOF\n$(git push --no-verify)\nEOF", SKIP),         # unquoted heredoc: substitutions run
    ("cat <<EOF\n`git reset --hard`\nEOF", RESET),
    ("A=$(git push --no-verify) echo safe", SKIP),
    ("(git commit -m safe; git push --no-verify)", SKIP),
    ("{ git push --no-verify; }", SKIP),
    ("if true; then git push --no-verify; fi", SKIP),
    ("for i in 1 2; do git reset --hard; done", RESET),
    ("while true; do git push --no-verify; done", SKIP),
    ("case x in x) git reset --hard ;; esac", RESET),
    ("case $x in a|b) rm -rf / ;; *) : ;; esac", RM),
    ("f() { git reset --hard; }; f", RESET),
    ("function f { rm -rf /; }; f", RM),
    ("! git reset --hard", RESET),
    ("true && (git reset --hard)", RESET),
    ("echo safe # git commit -m safe\ngit push --no-verify", SKIP),
    ("git commit -m x\r\ngit push --no-verify\r\n", SKIP),
    # -- the pre-existing rules, in forms the text regex could not see --
    ('rm -rf "$HOME"', RM),
    ("rm -rf '/'", RM),
    ("rm -rf -- /", RM),
    ("rm -rf / --no-preserve-root", RM),
    ("rm -rf $HOME/", RM),
    ("rm -r -f ~/", RM),
    ("rm --recursive --force /*", RM),
    ("rm --rec --for /", RM),
    ("rm -fR ~", RM),
    ("rm -rf ./a /", RM),
    ("rm -rf /etc /", RM),
    ("rm -rf ${HOME}/", RM),
    ("rm -f -r //", RM),
    ("/bin/rm -rf /", RM),
    ("\\rm -rf /", RM),
    ("command rm -rf /", RM),
    ("docker --context prod system prune -af", "docker-system-prune"),
    ("docker -H tcp://h:2375 system prune", "docker-system-prune"),
    ("sudo mkfs.ext4 /dev/sda1", "mkfs"),
    ("dd bs=1M if=/dev/zero of=/dev/nvme0n1", "dd-block-device"),
    ("sudo dd if=x of=/dev/sda", "dd-block-device"),
    ("chmod -R 0777 /", "chmod-root"),
    ("chmod --recursive 777 /", "chmod-root"),
    ("chmod -Rv 777 /*", "chmod-root"),
    ("sudo chmod -R 777 $HOME", "chmod-root"),
    ("rm \"$HOME/.ssh/id_rsa\"", "ssh-dir-mutation"),
    ("echo key | tee -a ~/.ssh/authorized_keys", "ssh-dir-mutation"),
    ("cat key >> $HOME/.ssh/authorized_keys", "ssh-dir-mutation"),
    ("echo x 2>~/.ssh/log", "ssh-dir-mutation"),
    ("echo x &> ~/.ssh/log", "ssh-dir-mutation"),
    ("sed -i s/a/b/ ~/.ssh/config", "ssh-dir-mutation"),
    ("sed -ni 's/a/b/p' ~/.ssh/config", "ssh-dir-mutation"),
    ("shred ~/.ssh/id_rsa", "ssh-dir-mutation"),
    ("chmod 644 ~/.ssh/id_rsa", "ssh-dir-chmod-open"),
    ("chmod -R a+rwx ${HOME}/.ssh", "ssh-dir-chmod-open"),
    ("sudo -u root chmod 777 ~/.ssh/id_rsa", "ssh-dir-chmod-open"),
    ("name(){ name|name& };name", "fork-bomb"),
    ("bash -c ':(){ :|:& };:'", "fork-bomb"),
    ("sh <<EOF\n:(){ :|:& };:\nEOF", "fork-bomb"),
    ("echo go; :(){ :|:& };:", "fork-bomb"),
    ("f(){ f|f& };f", "fork-bomb"),
]

# ── ALLOW: commands that must keep working ──────────────────────────────────────────────────────
ALLOW = [
    # -- the quoted mention is data (verify_guard.py) --
    'echo "never run git push --force origin master"',
    "echo 'git push --force origin master'",
    "echo git reset --hard",
    "echo rm -rf /",
    "printf '%s\\n' 'rm -rf /' 'git reset --hard'",
    'echo "git commit --no-verify"',
    "echo --no-verify && git commit -m \"msg\"",
    "grep -rn 'git push --force origin master' docs/",
    "grep -n 'rm -rf /' notes.txt",
    "rg 'SKIP_SECRET_SCAN=1' .",
    "echo SKIP_SECRET_SCAN=1",
    "cat README.md # then git push --force origin master",
    "man mkfs",
    "which mkfs.ext4",
    "command -v mkfs.ext4",
    "echo mkfs.ext4 /dev/sda1",
    "echo 'dd if=x of=/dev/sda'",
    "echo ':(){ :|:& };:'",
    'echo ":(){ :|:& };:" > note.txt',
    "python3 -c \"print('rm -rf /')\"",
    # -- git commit message / file text is data --
    "git commit -m 'fix: --no-verify edge case'",
    'git commit -m "fix: --no-verify edge case"',
    'git commit -m "Fixed -n bug in module"',
    'git commit -am "--no-verify"',
    'git commit -am "-n"',
    'git commit -m "doc: explain core.hooksPath= setting"',
    'git commit -m "doc: explain git push --no-verify risk"',
    'git commit -m "undo: git reset --hard and git push --force origin master"',
    'git commit -m "docs: SKIP_SECRET_SCAN=1 is forbidden"',
    "git commit --message=--no-verify",
    "git commit -m --no-verify",
    "git commit -F msg.txt",
    "git commit -F - < msg.txt",
    "git commit -mn",                       # `n` is the message, not a flag
    "git commit -tn -m msg",                # `n` is the template path
    "git commit -uno -m msg",               # -u<mode>
    "git commit -Sn -m msg",                # -S<keyid>
    'git commit --no-verbose -m "msg"',
    "git commit --amend --no-edit",
    "git commit -m 'one' -m 'two'",
    "git commit -m ok; grep -n needle file",
    "git commit -m x   ;   git commit --no-edit   ;   bash -n x.sh   ;   git commit -tn",
    "git commit -m x\nbash -n s.sh",
    "git commit -m x; sed -n 1p f",
    "git commit -m \"$(cat <<'EOF'\nfeat: never run git push --force origin master or git commit --no-verify\n\nSKIP_SECRET_SCAN=1 is denied too.\nEOF\n)\"",
    # -n means something else outside commit/am
    "git push -n origin feature",
    "git push --dry-run",
    "git merge -n topic",
    "git rebase -n main",
    "git cherry-pick -n abc123",
    "git log -n 10",
    "git log -n 10 && git commit -m 'msg'",
    "git diff -n",
    "git branch -n",
    "git commit -m ok && git push origin feature -n",
    # -- heredocs / files that merely contain denied text --
    "cat > notes.md <<'EOF'\ngit push --force origin master\ngit reset --hard\ngit commit --no-verify\nrm -rf /\nEOF",
    "cat >> report.md <<EOF\nthe agent ran dd and chmod -R and rm -r on files\ngit reset --hard is dangerous\nEOF",
    "python3 - <<'PY'\nprint('git commit -n')\nPY\nbash -n x.sh",
    "python3 <<EOF\nprint(\"git commit -q -m x\")\nEOF\nsed --no-verify file",
    "bash script.sh <<'EOF'\ngit commit -n -m x\nEOF",                  # fed to a script, not a shell program
    "bash -c 'cat' <<EOF\ngit commit -n -m x\nEOF",
    "cat <<EOF | tee out.txt\ngit reset --hard\nEOF",
    "cat <<'EOF' | grep reset\ngit reset --hard\nEOF",
    "tee notes.txt <<'EOF'\nrm -rf /\nEOF",
    "git commit -F - <<'EOF'\nrevert: git reset --hard\nEOF",
    "echo 'x' > a.sh\nbash a.sh",
    "cat <<A <<B\na\nA\ngit reset --hard\nB\n",                   # the second body is data too
    "cat <<-EOF\n\tgit reset --hard\n\tEOF\nls",
    "cat <<EOF\nno terminator, git reset --hard",
    "echo \"$(cat <<EOF\n)\ngit push --no-verify\nEOF\n)\"",
    "cat <<\"EOF\"\n$(git reset --hard)\nEOF",
    "echo $'a\\'b; git reset --hard'",
    "echo $((1<<3))",
    "(( i < 5 )) && echo yes",
    "x=$((a+b*2)); echo $x",
    "echo hi # git reset --hard\nls",
    "ls # don't run git reset --hard\nls",                          # a comment may hold a lone quote
    "case $x in a) ls;; b) echo git reset --hard;; esac",
    "cat <<'EOF'\n$(git reset --hard)\nEOF",                      # quoted delimiter: no expansion
    "cat <<EOF\n\\$(git reset --hard)\nEOF",                       # escaped
    "echo '`git push --no-verify`'",
    "echo \"\\`git push --no-verify\\`\"",
    "bash -c 'echo safe' 'git push --no-verify'",                  # extra words are $0/$1
    "bash +x -c 'echo safe' 'git push --no-verify'",
    "bash script.sh 'git push --no-verify'",
    "bash -n x.sh",
    # -- hook config reads and neutral values --
    "git config core.hooksPath",
    "git config --get core.hooksPath",
    "git config --global --unset core.hooksPath",
    "git config user.name igor",
    "git config --list",
    "git -c user.name=a -c core.pager=cat commit -m x",
    "git -c core.hooksPathX=1 status",
    "SKIP_SECRET_SCAN= git commit -m x",
    "SKIP_SECRET_SCAN=",
    "export SKIP_SECRET_SCAN=",
    "unset SKIP_SECRET_SCAN",
    "OTHER=1 git commit -m x",
    "SKIP_SECRET_SCAN_NOT=1 git commit -m x",
    # -- ordinary pushes / resets --
    "git push",
    "git push origin master",
    "git push origin HEAD:master",
    "git push -u origin master",
    "git push --tags",
    "git push origin +feature",
    "git push origin +HEAD:feature",
    "git push origin +refs/heads/feature:refs/heads/feature",
    "git push --force-with-lease origin feature-branch",
    "git push --force-with-lease -o ci.skip origin feature-branch",
    "git push --force-with-lease --force-if-includes origin feature-branch",
    "git push -f origin my-scratch-branch",
    "git push -f origin feature/main-fix",                         # `main` inside a word is not main
    "git push --force origin master:scratch",
    "git push --force origin HEAD",
    "git push --delete origin old-branch",
    "git push -o -f origin feature",                               # -f is the push option's value
    "git -C /tmp/x reset --soft HEAD~1",
    "git -C /tmp/x reset HEAD file",
    "git --git-dir=/x/.git status",
    "git -C /tmp/x status",
    "git reset --hard-looking-branch-name",                        # not a prefix of --hard
    "git reset -- --hard",
    "git checkout -b feature",
    "git stash",
    "git status",
    "git diff HEAD~1",
    # -- ordinary wrappers / interpreters --
    "sudo ls /root",
    "sudo apt-get update",
    "sudo -u deploy systemctl status app",
    "env FOO=1 ls",
    "env",
    "xargs echo",
    "nohup sleep 100 &",
    "timeout 10 ls",
    "nice -n 5 make",
    "time make",
    "command -v git",
    "bash -c 'echo hi'",
    "sh -c 'ls -la'",
    "eval echo hi",
    "ssh host ls -la",
    "ssh -p 22 host",
    "ssh host 'rm -rf /tmp/build'",
    "ssh host 'git status'",
    "find . -name '*.pyc' -delete",
    "find . -name '*.pyc' -exec rm -f {} +",
    "su - deploy",
    "rm -rf /tmp/build",
    "rm -rf ./build /tmp/x",
    "rm -rf -- ./build",
    "rm -rf $HOME/cardloop/tmp",
    "rm -r /",                                                     # no -f: unchanged behaviour
    "rm -f /",
    "ls / ~ $HOME",
    "chmod -R 755 /home/alice/myproject",
    "chmod 777 /tmp/x",
    "chmod -R 777 ./dist",
    "chmod 600 ~/.ssh/id_ed25519",
    "chmod 000 $HOME/.ssh/id_rsa",
    "chmod u-w ~/.ssh/config",
    "cat ~/.ssh/config",
    "ls -la ~/.ssh",
    "ssh-keygen -t ed25519 -f ~/.ssh/id_new",
    "grep Host ~/.ssh/config > /tmp/hosts",
    "echo hi 2>/dev/null",
    "echo hi >&2",
    "docker system df",
    "docker container prune",
    "docker compose down",
    "docker ps",
    "dd if=/dev/zero of=testfile bs=1M count=10",
    "dd if=/dev/sda of=backup.img",
    "dd if=/dev/zero of=/dev/null",
    "make -j4 && make install",
    "mkfsomething arg",
    # -- syntax that must not trip the lexer --
    "echo a#b",
    "echo ${HOME} ${x:-default} ${#var} $((1+2)) $(date)",
    "echo ${x:-$(echo fallback)}",
    "echo 'it'\"'\"'s'",
    "echo \"a $(echo 'b)') c\"",
    "cat file | grep a | sort -u > out; echo done",
    "[[ -f x && $y == z ]] && echo ok || echo no",
    "(cd /tmp && ls)",
    "{ echo a; echo b; } > out",
    "a=1 b=2 c=$(echo 3) env | sort",
    "echo $'tab\\there'",
    "echo \"multi\nline\"",
    "ls \\\n -la",
    "diff <(sort a) <(sort b)",
    "VAR=\"git reset --hard\"; echo $VAR",
    "x=`echo hi`",
    "",
    "   ",
    "# just a comment",
]


@pytest.mark.parametrize("cmd,expected", DENY)
def test_denied(cmd, expected):
    assert rule_of(cmd) == expected, f"{cmd!r}: expected {expected}, got {rule_of(cmd)}"


@pytest.mark.parametrize("cmd", ALLOW)
def test_allowed(cmd):
    assert rule_of(cmd) is None, f"{cmd!r} was denied by {rule_of(cmd)}"


def test_rule_ids_are_stable_and_documented():
    # every rule a classifier path can return is declared with a reason; ids are the audit key
    assert set(cg.RULES) == {
        "fork-bomb", "rm-root-home", "chmod-root", "git-force-push-protected", "git-hard-reset",
        "ssh-dir-mutation", "ssh-dir-chmod-open", "docker-system-prune", "mkfs",
        "dd-block-device", "git-skip-hooks", "git-hookspath", "skip-secret-scan"}
    assert all(r for r in cg.RULES.values())
    seen = {expected for _cmd, expected in DENY}
    assert seen == set(cg.RULES), f"rules without a deny case: {set(cg.RULES) - seen}"


# ── never raises, whatever it is given ───────────────────────────────────────────────────────────
GARBAGE = [None, 0, 12345, b"git commit -n", [], "\x00\x01 rm -rf /", "\x00" * 100, "a" * 5000,
           "'", '"', "`", "$(", "${", "<<", "<<EOF", "cat <<EOF", "((", "))", ";;", "|", "&", ">", "&&&&",
           "\\", "$'", "$'\\", "case", "case x in", "esac )", ") )", "( ( (", "{ { {", "}}}",
           "a=" * 1000, "echo '" * 1000, '"' * 1001, "$(" * 200, "`" * 301, "<(" * 100]


@pytest.mark.parametrize("junk", GARBAGE, ids=lambda j: repr(j)[:30])
def test_never_raises(junk):
    cg.classify_command(junk)
    engine._classify_dangerous_command(junk)
    engine._classify_dangerous_command_rule(junk)


# ── time: linear, bounded, and the budget fallback ───────────────────────────────────────────────
def _timed(cmd, limit):
    t = time.perf_counter()
    cg.classify_command(cmd)
    dt = time.perf_counter() - t
    assert dt < limit, f"{len(cmd)} bytes took {dt:.3f}s (limit {limit}s): {cmd[:40]!r}"
    return dt


def test_repeated_tokens_are_fast():
    # measured before the rewrite: 24 KB of "dd " = 2.67 s; the targets are 100 ms / 50 ms,
    # the asserts leave a generous CI margin
    _timed("dd " * 8000, 0.5)
    _timed("dd " * 24000, 0.5)                           # 72 KB of one token
    _timed("rm -r " * 4000, 0.5)
    _timed("rm -r " * 12000, 0.5)
    _timed("chmod -R " * 8000, 0.5)
    _timed("git push --force " * 4000, 0.5)
    _timed("git commit -a " * 5000, 0.5)
    _timed("'a' " * 18000, 0.5)
    _timed("a;" * 36000, 0.5)
    _timed("a | " * 18000, 0.5)
    _timed("$(a) " * 14000, 0.5)
    _timed("a" * 72000, 0.5)
    _timed("x" * 100 + " " + "-" * 72000, 0.5)


def test_heredoc_bodies_are_fast():
    prose = "the agent ran dd and chmod -R and rm -r on files " * 800
    _timed("cat >> r.md <<EOF\n" + prose + "\nEOF", 0.5)                       # 40 KB
    _timed("cat >> r.md <<'EOF'\n" + prose + "\nEOF", 0.5)
    lines = "\n".join("git reset --hard # " + str(i) for i in range(2000))
    _timed("cat > x <<'EOF'\n" + lines + "\nEOF", 0.5)
    _timed("cat > x <<'EOF'\n" + "a\n" * 36000 + "EOF", 0.5)
    py = Path(ROOT / "webapp.py").read_text()[:60000]
    _timed("cat > /tmp/x.py <<'EOF'\n" + py + "\nEOF", 0.5)


def test_pathological_shapes_are_bounded():
    _timed(":(){ " * 8000, 1.0)
    _timed("( " * 20000, 1.0)
    _timed("${" * 20000, 1.0)
    _timed("<<EOF " * 8000, 1.0)
    _timed('"' * 70001, 1.0)
    _timed("\\\n" * 30000, 1.0)
    _timed("$(" * 100 + "x" + ")" * 100, 1.0)
    _timed("bash -c '" * 100 + "x" + "'" * 100, 1.0)
    _timed("echo `" * 5000, 1.0)
    _timed("case x in " * 8000, 1.0)
    _timed("a=1 " * 18000, 1.0)
    _timed("git -c " * 12000 + "status", 1.0)
    _timed("env " * 18000 + "ls", 1.0)
    _timed("sudo " * 14000 + "ls", 1.0)
    # arithmetic guesses must never be re-tried at every nesting level (2**depth work)
    _timed("((" * 30000, 1.0)
    _timed("$((" * 20000, 1.0)
    _timed("$(( " * 15000, 1.0)
    _timed("$((" * 24 + "1" + "))" * 24, 1.0)
    _timed("$((" * 23 + "1;" + ")" * 23, 1.0)
    _timed("$(( $((" * 12 + "1" + ")) ;" * 12, 1.0)
    _timed("echo $(( " * 4000 + "x" + " ))" * 4000, 1.0)


def test_unbalanced_quotes_fall_back_to_the_narrow_scan():
    # a shell would refuse this text, the lexer cannot parse it: only deny shapes still deny
    assert rule_of('echo "unterminated; rm -rf /') == RM
    assert rule_of("echo 'it; git reset --hard") == RESET
    assert rule_of('git commit -m "oops --no-verify') == SKIP
    assert rule_of('echo "oops; SKIP_SECRET_SCAN=1 git commit') == SCAN
    assert rule_of('git push origin +master "') == PUSH
    assert rule_of("x 'a; git -c core.hooksPath=/x commit") == HOOKS
    assert rule_of("echo 'unterminated; docker system prune") == "docker-system-prune"
    assert rule_of("echo \"unterminated; sudo mkfs.ext4 /dev/sda") == "mkfs"
    assert rule_of("echo 'unterminated; > ~/.ssh/authorized_keys") == "ssh-dir-mutation"
    # ... and everything else still runs
    assert rule_of('echo "it\'s unterminated; ls -la') is None
    assert rule_of("echo 'oops; git status; git push origin feature") is None
    assert rule_of('git commit -m "it works') is None


def test_oversized_input_falls_back_to_the_narrow_scan():
    big = "echo hello world\n" * (cg.MAX_CHARS // 17 + 10)
    assert len(big) > cg.MAX_CHARS
    assert rule_of(big) is None
    assert rule_of(big + "git reset --hard") == RESET
    assert rule_of("rm -rf /\n" + big) == RM
    assert rule_of(big + "git push --force origin main") == PUSH
    assert rule_of(big + "git commit --no-verify -m x") == SKIP
    assert rule_of(big + "SKIP_SECRET_SCAN=1 git commit -m x") == SCAN
    assert rule_of(big + "git push origin +master") == PUSH
    assert rule_of(big + "git -C x -c core.hooksPath=/y commit") == HOOKS
    assert rule_of(big + 'echo "git status" | cat') is None
    t = time.perf_counter()
    rule_of(big + "git status")
    assert time.perf_counter() - t < 1.5


def test_nesting_deeper_than_the_limit_falls_back():
    deep = "echo " + "$(" * 120 + "git push --no-verify" + ")" * 120
    assert rule_of(deep) == SKIP
    assert rule_of("echo " + "$(" * 120 + "ls" + ")" * 120) is None
    # programs inside programs: a shell fed a heredoc that feeds a shell ... (linear text)
    def layered(levels, inner):
        text = inner + "\n"
        for k in range(levels):
            text = f"bash <<L{k}\n{text}L{k}\n"
        return text
    assert rule_of(layered(5, "git reset --hard")) == RESET
    assert rule_of(layered(40, "git reset --hard")) == RESET        # past MAX_DEPTH: narrow scan
    assert rule_of(layered(40, "ls -la")) is None
    moderate = "echo " + "$(" * 8 + "git push --no-verify" + ")" * 8
    assert rule_of(moderate) == SKIP


def test_work_budget_stops_amplification():
    # each bash -c layer re-parses its payload: the shared work budget must cut it off, and the
    # narrow fallback still names the shape
    text = "git reset --hard\n"
    for k in range(20):
        text = f"bash <<L{k}\n{text}" + "x " * 2000 + f"\nL{k}\n"
    t = time.perf_counter()
    got = rule_of(text)
    assert time.perf_counter() - t < 1.5
    assert got == RESET                    # found by the budget fallback at the latest


# ── keep-existing behaviours ─────────────────────────────────────────────────────────────────────
def test_force_with_lease_keeps_its_old_semantics():
    assert rule_of("git push --force-with-lease origin main") == PUSH
    assert rule_of("git push --force-with-lease") == PUSH                 # unqualified
    assert rule_of("git push --force-with-lease origin") == PUSH          # remote only
    assert rule_of("git push --force-with-lease origin feature-branch") is None
    assert rule_of("git push -f") == PUSH
    assert rule_of("git push -f origin") == PUSH
    assert rule_of("git push origin --force") == PUSH
    assert rule_of("git push -f origin my-scratch-branch") is None


def test_remote_commands_are_still_scanned():
    # the old text matcher saw through `ssh host '<cmd>'`; the tokenizer parses the remote line
    assert rule_of("ssh host git push --force origin master") == PUSH
    assert rule_of('ssh host "git push --force origin master"') == PUSH
    assert rule_of("ssh host 'echo ok; rm -rf /'") == RM
    assert rule_of("ssh host 'echo \"rm -rf /\"'") is None


# ── the engine entry points ──────────────────────────────────────────────────────────────────────
def test_engine_wrappers_agree_with_the_classifier():
    cmd = "git commit --no-verify -m x"
    assert engine._classify_dangerous_command_rule(cmd) == (SKIP, cg.RULES[SKIP])
    assert engine._classify_dangerous_command(cmd) == cg.RULES[SKIP]
    assert engine._classify_dangerous_command("git status") is None
    assert engine._classify_dangerous_command_rule("git status") is None


def test_extra_deny_patterns_still_apply(monkeypatch):
    import re
    monkeypatch.setattr(engine, "_DENY_COMMAND_PATTERNS_EXTRA", [re.compile(r"kubectl\s+delete\s+ns\s+prod")])
    rule, reason = engine._classify_dangerous_command_rule("kubectl delete ns prod --now")
    assert rule == "extra-pattern" and "DENY_COMMANDS_EXTRA" in reason
    assert engine._classify_dangerous_command_rule("kubectl get ns") is None
    # a built-in rule wins over an extra pattern
    assert engine._classify_dangerous_command_rule("git reset --hard")[0] == RESET


@pytest.fixture
def audit_log(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "audit", lambda project, kind, text: calls.append((project, kind, text)))
    return calls


def _hook_input(command, cwd=None):
    out = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd:
        out["cwd"] = cwd
    return out


def test_every_deny_is_audited_with_rule_id_and_project(audit_log):
    hook = engine._make_dangerous_command_guard_hook("myproj")
    out = asyncio.run(hook(_hook_input("git commit --no-verify -m x"), None, None))
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert "git-skip-hooks" in spec["permissionDecisionReason"]
    assert audit_log == [("myproj", "DENY", "git-skip-hooks: git commit --no-verify -m x")]


def test_allow_writes_no_audit_line(audit_log):
    hook = engine._make_dangerous_command_guard_hook("myproj")
    assert asyncio.run(hook(_hook_input("git commit -m 'fix: --no-verify'"), None, None)) == {}
    assert asyncio.run(hook(_hook_input("ls -la"), None, None)) == {}
    assert asyncio.run(hook(_hook_input(""), None, None)) == {}
    assert audit_log == []


def test_audit_command_is_cut_and_flattened(audit_log):
    hook = engine._make_dangerous_command_guard_hook("p")
    long_cmd = "git reset --hard " + "x" * 500 + "\nsecond line"
    asyncio.run(hook(_hook_input(long_cmd), None, None))
    (_project, kind, text), = audit_log
    assert kind == "DENY" and text.startswith("git-hard-reset: git reset --hard xxx")
    command_part = text.split(": ", 1)[1]
    assert len(command_part) <= 200 and "\n" not in text
    assert command_part.endswith("…")


def test_hook_without_project_uses_the_cwd(audit_log):
    asyncio.run(engine._dangerous_command_guard_hook(_hook_input("rm -rf /", cwd="/srv/apps/shop/"), None, None))
    asyncio.run(engine._dangerous_command_guard_hook(_hook_input("rm -rf /"), None, None))
    assert [a[0] for a in audit_log] == ["shop", "unknown"]
    assert all(a[1] == "DENY" and a[2].startswith("rm-root-home: ") for a in audit_log)


def test_hook_reason_says_a_retry_will_not_pass():
    out = asyncio.run(engine._dangerous_command_guard_hook(_hook_input("SKIP_SECRET_SCAN=1 git commit -m x"), None, None))
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "skip-secret-scan" in reason and "denied again" in reason


def test_hook_handles_a_large_command_off_the_event_loop(audit_log):
    big = "echo hello\n" * 5000 + "git push origin +master"
    assert len(big) > engine._GUARD_OFFLOAD_CHARS
    out = asyncio.run(engine._make_dangerous_command_guard_hook("p")(_hook_input(big), None, None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert audit_log and audit_log[0][2].startswith("git-force-push-protected: ")


def test_hook_is_registered_per_run_with_the_project():
    src = (ROOT / "engine.py").read_text()
    assert "_make_dangerous_command_guard_hook(project_name)" in src
    assert "hooks=[_bundle_grep_guard_hook, _bash_deny_hook]" in src
