WIJERCO COMMAND CENTRE UPDATE - 16 AUG 2026 (v2)
=============================================

WHY THIS IS A v2
You ran deploy.bat and the UI didn't change. Two real problems, both fixed here:

1. The UI fix itself was incomplete. The Governance/Memory merge I did
   earlier only touched one nav list, but this build (the "Apex" orbital
   nav) has a second, separate list (APX_NODES) that still had Governance
   and Memory as their own ring items. That's the one actually rendered,
   so nothing visibly changed no matter how deploy.bat ran. Found and
   fixed - both entries are gone from the real nav array now, verified
   live against my own dev machine's orchestrator before repackaging.

2. I can't tell what deploy.bat actually did on your end - the old
   version didn't log anything to a file and the window likely closed
   before you could read it. This version logs everything to
   deploy_log.txt next to itself, and pauses at the end so the window
   stays open. If step 5 or 6 shows anything odd, send me deploy_log.txt.

WHAT'S IN IT
  command_centre.html, sw.js   - the real fix, cache bumped to v7
  docker-compose.yml            - unchanged from last time
  remote_deploy.json            - the remote-deploy webhook workflow
  remote_deploy_credentials.json - webhook auth credential, deleted by
                                    deploy.bat right after import
  deploy.bat                    - run this, watch or send me the log

ONE THING I ALREADY KNOW FROM THIS END
I tried using the webhook from my last package to push this fix directly,
before repackaging this way - it came back "workflow is not registered."
That means either deploy.bat didn't reach step 5 on your machine last
time, or it ran into a problem there. This version gives both of us more
to go on if it happens again.

WHAT TO DO
Run deploy.bat, let it finish (it now waits properly for n8n to boot
before talking to it), then either confirm the Command Centre looks right
after a hard refresh (Ctrl+F5), or send me deploy_log.txt if it doesn't.
