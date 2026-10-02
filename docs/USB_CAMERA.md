# Optional USB camera preview

Run SDL Camera Server on the camera's host and configure a camera-scoped token.
Set `OT2_CAMERA_SERVICE_CONFIG` to an absolute path to a private JSON file:

```json
{"url":"http://127.0.0.1:8070","token":"REPLACE_WITH_CAMERA_SCOPED_TOKEN","cameras":{"overhead":"ot2_hte_overhead"}}
```

Restart the gateway after configuring the optional router. The gateway exposes
camera listing, status, JPEG snapshots, and MJPEG under `/cameras`. Existing
identity checks apply; camera access does not acquire a robot claim or move it.
Without this configuration the Camera button opens a “No camera stream configured” message.

The Camera button next to Light opens a floating preview. Drag its title to move
it and its bottom-right corner to resize it; keyboard arrows work on both handles.
Close with ×, Escape, or the Camera button. Closing cancels this viewer's requests
and releases its images; it does not stop other consumers. With no consumers,
the camera service releases the physical camera after its configured idle timeout.

The preview makes one snapshot request at a time, at most five per second. It
starts only on click, pauses when the browser document is hidden, and stops on an
error with an explicit Retry action. No camera credentials are sent to the browser.

## Complexation Tapo camera

The `ot2_complexation` panel uses the existing dashboard-registered Tapo C100
feed `cam_echem_tapo_c100_main` (Echem OT2 Camera). It opens in the same movable,
resizable Camera window. The browser requests a viewing lease from the same-origin
`/api/camera-streams/sessions` broker; it never connects directly to RTSP or
receives camera credentials. Use the authenticated dashboard edge URL.

The player and lease helpers are adapted from `ac-organic-lab/web/src` at
`257c265`. They retain broker authorization, capacity admission, ticket-first
WebSocket authentication, heartbeats, release on close, hidden-tab suspension,
bounded buffering and reconnects, and MSE/WebRTC browser support. Keep these
helpers aligned when the dashboard broker protocol changes. Offline tests cover
admission refusal and lease cleanup. This integration needs no changes to the
shared camera service or HTE's USB configuration.
