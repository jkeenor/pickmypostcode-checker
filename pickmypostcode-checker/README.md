# Pick My Postcode Checker

This is a small containerized checker for Portainer.

It watches the live Pick My Postcode current-draw API using a postcode you provide in the stack, then runs once per day at the time you configure.

## Stack env vars

- `POSTCODE`: Your postcode to check.
- `HOST_PORT`: Host port to expose the dashboard on. Default is `8099`.
- `CHECK_TIME`: Daily check time in `HH:MM` or `HH:MM:SS`.
- `TZ`: Timezone used for the schedule. Default is `Europe/London`.

Optional overrides:

- `ENTRY_ID`: Current draw entry id used by the live API. Default is `27079`.
- `CHECK_URL_TEMPLATE`: URL to check. Default is `https://pickmypostcode.com/api/index.php/entry/current/{entry_id}`
- `PUSHOVER_APP_TOKEN`: Pushover application token used to send notifications.
- `PUSHOVER_USER_KEY`: Your Pushover user key or group key.
- `PUSHOVER_DEVICE`: Optional device name to target.
- `PUSHOVER_SOUND`: Optional Pushover sound name.
- `PUSHOVER_TITLE`: Notification title. Default is `Pick My Postcode`.
- `PUSHOVER_URL`: Optional URL to attach to the notification.
- `PUSHOVER_URL_TITLE`: Optional label for the attached URL.
- `REQUEST_TIMEOUT`: Seconds before the request fails.

## Endpoints

- `/`: simple status dashboard
- `/health`: JSON health check
- `/api/status`: JSON snapshot

## Notes

The default target uses the public current-draw API on Pick My Postcode. If the site changes its API path or draw id, you can change `CHECK_URL_TEMPLATE` or `ENTRY_ID` without changing the code.

The login page on the site is a normal WordPress login, so it expects a username/email plus password. This checker does not need to log in to read the public current-draw data.

If `PUSHOVER_APP_TOKEN` and `PUSHOVER_USER_KEY` are set, the container sends a Pushover notification the first time it sees your postcode in the current draw. It stores that notification signature in the persisted state volume so the same winning result does not trigger repeated alerts every day.
