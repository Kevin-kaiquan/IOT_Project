# HTTP API reference

The API returns JSON except for the camera frame endpoint. It currently has no
authentication and should only be used on a trusted network.

## Read the current snapshot

```http
GET /api/data
```

Returns `now`, recent `history`, device states, manual overrides, atomizer
state, camera detection data, and the active CO₂ source.

## Read camera status

```http
GET /api/camera/status
```

Example response:

```json
{
  "ok": true,
  "cameras": [
    {"id": 0, "ready": true},
    {"id": 1, "ready": false}
  ]
}
```

## Read a camera frame

```http
GET /api/camera/0/frame
```

Returns `image/jpeg`. Invalid camera IDs return 404; unavailable cameras return
503.

## Read device state

```http
GET /api/control
```

Example response:

```json
{
  "ok": true,
  "devices": {
    "heater": "off",
    "fan": "on",
    "led": "off",
    "atomizer": "off"
  },
  "overrides": {
    "fan": {
      "state": "on",
      "expires_at": 1760000000.0
    }
  }
}
```

## Set a manual override

```http
POST /api/control
Content-Type: application/json
```

```json
{
  "device": "fan",
  "state": "on",
  "duration_sec": 300
}
```

`device` must be `heater`, `fan`, `led`, or `atomizer`. `state` must be `on`
or `off`. `duration_sec` is optional and defaults to 300 seconds.

## Set atomizer state directly

```http
POST /api/atomizer
Content-Type: application/json
```

```json
{"state": "on"}
```

The endpoint also accepts `GET /api/atomizer?state=on`. This direct endpoint
does not create a timed override; automatic humidity control may change the
state on the next control cycle. Use `/api/control` for a timed hold.

## Flash OLED text

```http
GET /api/oled/text?text=Hello&sec=2
```

Returns 503 when OLED support is disabled or unavailable.

