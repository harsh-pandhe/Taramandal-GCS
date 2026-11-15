# Fleet discovery and the heartbeat watchdog (issue #58)

An **opt-in** way for drones to announce themselves to the GCS by hardware id, and for the GCS to notice when one goes
quiet. It is off by default and changes nothing about how the GCS flies a show.

## What it does
- Drones send a 30-byte authenticated **announcement** (about 2 Hz) to a UDP port on the GCS.
- The GCS keeps a registry `hardware_uuid -> drone_id` (the drone id is the launch pad index). A new id gets the lowest free
  slot. **A drone it has seen before always gets its original slot back**, including after a GCS restart (the bindings are
  saved to a small file).
- A drone that stops announcing is marked **STALE** after 1.5 s (three missed heartbeats at 2 Hz) and is never shown as online
  again until a verified announcement arrives. Nothing is inferred: after a GCS restart every remembered drone is STALE
  until it is heard.
- `GET /api/fleet/registry` (any signed-in operator) lists each drone's id, pad, hardware id, state and age, the counts, how
  many frames were rejected and why, and the last 100 joined/stale/recovered events. `{"enabled": false}` when it is off.

## What it does not do (this slice)
- It does **not connect** to a discovered drone, create telemetry for it, or touch `DroneManager`. A discovered drone is in
  the registry; wiring it into the flight path is a separate, later decision. Today's per-drone freshness rule
  (`HEARTBEAT_STALE_S`, #131) is unchanged and independent.
- It does not make any flight decision, and a STALE drone does not trigger an abort.
- No LoRa/serial transport (that is #67/#68); this listens on UDP.

## Turning it on
```
GCS_DISCOVERY=1
GCS_DISCOVERY_KEY_FILE=/path/to/key     # a file holding the key as hex (at least 32 hex characters); or GCS_DISCOVERY_KEY=<hex>
GCS_DISCOVERY_HOST=127.0.0.1            # default; use 0.0.0.0 to hear a subnet
GCS_DISCOVERY_PORT=14590                # default
GCS_DISCOVERY_STALE_S=1.5               # default
GCS_DISCOVERY_STORE=...                 # where slots are remembered; default fleet_registry.json beside show_state.json
```
Make a key with `python -c "import secrets; print(secrets.token_hex(32))" > discovery.key`. **It refuses to start without a
key** (it would be an unauthenticated way to put drones into the fleet): the GCS logs why and starts without discovery. The
same happens for a bad setting or a port that is already taken.

Try it by hand with the fake drone:
```
python tools/fake_drone_announce.py --key-hex $(cat discovery.key) --uuid 0xA1B2C3D4E5F60718
curl -H "Authorization: Bearer <token>" localhost:8090/api/fleet/registry
```

## The frame (`tara_gcs/fleet_registry.py`)
`magic "TANN" (4) | version (1) | flags (1) | hardware_uuid u64 | timestamp_usec u64 | tag (8)`, little-endian. The tag is
HMAC-SHA256 under the key, with its own domain prefix, truncated to 8 bytes, so a tag for a broadcast-downlink frame never
validates an announcement (and the reverse). A frame is dropped if it is the wrong size, fails the tag, carries a clock more
than 0.5 s from the GCS wall clock, or is not newer than the last accepted frame from that drone. A dropped frame changes
nothing, and is counted by reason.

The timestamp is the anti-replay value (not a counter), so a drone that reboots is still accepted as long as its clock keeps
running. As with the broadcast receiver, a drone needs a clock independent of this link, such as GPS time.

## Limits to know about
- **One shared key.** Whoever holds the key can announce as any hardware id. It stops an outsider on the network, not a
  compromised drone. Per-drone keys would be the next step.
- **Replay window.** A frame the GCS never received can be replayed within the 0.5 s skew window. A frame it did receive
  cannot, and a replay cannot revive a drone that has gone stale for longer.
- A hardware id that two physical drones share would interleave their announcements; the replay rule drops the older one's
  frames, so one of them will look flaky rather than the registry being confused.
- Slots are kept for drones that are off, so a 1,000-drone fleet that loses a drone for good needs `release()` (not exposed
  over the API yet) to free its slot.
- Timings were measured on one laptop with real processes (`test_acceptance_...`): discovery well under 1 s, STALE under 2 s,
  slot reclaimed, and the test held 15 of 15 runs with all cores saturated. Real radios drop packets; at 2 Hz and a 1.5 s
  limit, three consecutive lost frames make a healthy drone read STALE until the next one arrives.
