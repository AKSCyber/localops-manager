# Sync LocalOps between Windows computers

This source snapshot includes the computer selector, node registration, and checks that prevent commands from reaching the wrong computer. Each computer runs its own LocalOps server and retains its own authentication and app registry. Selecting a computer opens its LocalOps address; sign in separately on each computer.

## Update an existing installation

1. Clone this private repository into a separate folder using your GitHub account. Do not clone over the existing running installation.
2. Stop LocalOps on the target computer using its existing stop launcher. If it runs elevated, use an elevated PowerShell window. Pause its startup task while updating.
3. From the cloned repository, run:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\Update-Installation.ps1 -Target 'C:\path\to\existing\localops-manager'
   ```

   The helper backs up the files it replaces and copies only core Python code and static assets. It preserves the target's data, credentials, schedules, vendor tools, virtual environment, and machine-specific launchers. It refuses to update while port 8790 is listening.
4. On the old computer, set `data/node-config.json` to its own dedicated HTTPS hostname, for example `{"publicHost":"localops-old.example.com"}`. Configure that hostname's tunnel to this computer's `http://127.0.0.1:8790`. The example hostname must be replaced with a real configured hostname.
5. Start LocalOps with that computer's existing launcher. Confirm `/health` and the displayed computer name. In the dashboard choose **Add computer**, entering the other computer's exact Windows computer name (`hostname`) and dedicated HTTPS URL. Register both directions if you want to switch from either dashboard.
6. For future updates, run `git pull --ff-only` in the clone, stop LocalOps, run the update helper again, and restart.

Do not give both computers the same tunnel hostname: a shared address cannot reliably select a computer. Do not copy `data/` between computers: Windows DPAPI secrets belong to the Windows account that created them.

## What is included

Application code, static UI, tests, and launchers from the new machine are included. The launchers in this repository contain deployment-specific paths and must be reviewed before use on another machine. The update helper intentionally preserves the old machine's launchers. `README.md` contains historical deployment details; it does not imply startup tasks have been installed on a fresh clone.

Runtime data, credentials, logs, virtual environments, and third-party vendor binaries are excluded. Existing vendor tools remain usable on an updated installation. Fresh installations need Python, `pip install -r requirements.txt`, their own app registry and authentication setup; optional CPU temperature monitoring also needs the vendor sensor tools described in README.

## Roll back

Stop LocalOps, copy the core files and static directory from the timestamped sibling backup folder back into the installation, then restart with its existing launcher. Each backup contains only files replaced by the helper.
