#!/bin/bash
# Substitute your GitHub username everywhere, fix the commit authorship,
# and print the push commands.
#
#   ./finish-and-push.sh YOUR_GITHUB_USERNAME
#
set -e
U="$1"
[ -z "$U" ] && { echo "usage: $0 <github-username>"; exit 1; }
cd "$(dirname "$0")"

# LICENSE copyright line and any README links
grep -rl 'NautyXie' . --exclude-dir=.git | while read -r f; do
    sed -i '' "s|NautyXie|$U|g" "$f" 2>/dev/null || sed -i "s|NautyXie|$U|g" "$f"
done

# author the commit as you, with GitHub's no-reply address so no real
# email address ends up in the public history
git config user.name "$U"
git config user.email "$U@users.noreply.github.com"
git add -A
git -c user.name="$U" -c user.email="$U@users.noreply.github.com" \
    commit -q --amend --reset-author --no-edit

cat <<TXT

Done. The repo is ready at:
  $(pwd)

You may want to move it out of the KiCad project folder first:
  mv "$(pwd)" ~/pcap04-capacitance-readout && cd ~/pcap04-capacitance-readout

Now create an EMPTY repo on github.com (no README, no .gitignore, no licence
- this repo already has them), then:

  git remote add origin git@github.com:$U/pcap04-capacitance-readout.git
  git push -u origin main

or over HTTPS:

  git remote add origin https://github.com/$U/pcap04-capacitance-readout.git
  git push -u origin main

TXT
