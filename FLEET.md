# One dashboard for multiple computers

LocalOps 1.6.0 adds an **All computers** dashboard. Each installation can be a dashboard server, a controlled node, or both. The browser communicates only with the dashboard it is signed into. That server fetches status and sends confirmed app commands to paired nodes over verified HTTPS.

## Installation and pairing

1. Update both Maximus and Helios to this version. Use `Update-Installation.ps1` from a separate extracted copy while LocalOps is stopped. It backs up replaced source files and preserves each computer's data, authentication, app registry, environment, vendor tools, and launchers.
2. Give each computer a dedicated HTTPS hostname that routes to its own loopback LocalOps service. Confirm the hostname in `data/node-config.json` (`publicHost`) or `LOCALOPS_PUBLIC_HOST`. Shared/load-balanced hostnames cannot reliably address a machine.
3. On Helios, open its local dashboard and click **Share this computer**. Enter a label such as `Maximus dashboard`. Copy the machine ID and the one-time displayed access key.
4. On Maximus, click **Connect computer**, enter Helios's dedicated HTTPS address, machine ID, and key. Pairing verifies the remote identity before saving anything.
5. Helios now appears alongside Maximus with application states, telemetry, and per-app start/stop/restart buttons. Sign into Maximus's online dashboard to use both together. No Helios password is sent through the browser during normal fleet use.

Pair the other direction only if you also want Helios to serve as a dashboard for Maximus. Closing the access-key dialog clears the displayed key; create another if needed. Each key expires after 90 days. Revoke keys under **Access granted to other dashboards** on the node that issued them. Disconnect removes a peer from a dashboard; it does not revoke the key on the remote machine.

## Identity and data

`data/machine-identity.json` contains a randomly generated UUID created once per installation. It survives Windows computer renames. Preserve it when updating or restoring the same computer. Do not copy that file to a different machine. Old hostname-derived selector links need to be reopened with the new machine ID after this upgrade.

`data/fleet-grants.json` stores only hashes of issued keys, labels, and expiry times. `data/fleet-peers.json` stores paired addresses and keys protected with Windows DPAPI under the account running LocalOps. Run pairing and the service under the same Windows account. These files and all other runtime data are excluded from Git and release archives.

## Controls and connection behavior

Every node validates the bearer key and exact machine ID before responding or executing a command. The receiving node repeats its existing process ownership checks. Fleet keys allow status and application start/stop/restart only. Registry editing, schedules and PC power controls remain in the individual machine's detailed tools and retain their own authentication requirements.

Remote status calls run in parallel with an eight-second timeout and a short cache. Unreachable nodes show no active controls or stale application data. Control requests are never retried automatically; after a timeout inspect the target before submitting again because the first command may have executed. Redirects are refused so keys cannot be forwarded to another destination. TLS certificate validation remains enabled.

The detailed-view selector remains available for older registered links, but the **All computers** section is the combined interface. Existing online password/TOTP authentication remains in place: this upgrade does not reset or repair administrator credentials. Local loopback access retains the existing trust model, so only trusted software and users should access the Windows account running LocalOps.

## Validation

Run `python -m unittest discover -s tests -v` from the project root with requirements installed. The private deployment registry check is skipped in source-only checkouts. Two process-control integration tests use disposable servers and leave other managed applications untouched. GitHub Actions runs the suite on Windows.
