# Example Use Cases

Real-world tasks for `/config/llm_tasks.yaml`. For the basics and the full list of task keys, see the [README](README.md#example-tasks).

## Smart doorbell: who is at the door?

When someone rings, the add-on grabs a snapshot from a Hikvision door station via its ISAPI endpoint and lets the LLM describe the visitor in one short sentence. Push it to your phone, or have an Amazon Echo announce it.

### Task

```yaml
tasks:
  doorbell:
    name: "Front Door Visitor"
    icon: mdi:doorbell-video
    entities:
      # A snapshot URL is attached as an image (user:password@ is supported)
      front_door: "http://doorbell:password@192.168.10.41/ISAPI/Streaming/channels/101/picture"
    prompt: |
      You are a doorbell with voice output. Someone has just rung the bell.
      Describe in at most 20 words who is at the door, for a push notification.
      Recognise delivery services (DHL, Amazon, etc.) and the shipment size (letter, parcel).
      For unknown people, give gender and estimated age (e.g. "man, about 40").
      Ignore our own cars (white van, black BYD).
      If nobody is at the door, answer: "Nobody is at the door."
      No introductory phrases.
```

Result: `sensor.front_door_visitor` holds the answer, e.g. `DHL courier with a medium-sized parcel.`

### Automation

Replace the doorbell trigger and the notify service with your own entities:

```yaml
automation:
  - alias: "Doorbell: describe visitor"
    triggers:
      - trigger: state
        entity_id: binary_sensor.front_door_doorbell
        to: "on"
    actions:
      - action: mqtt.publish
        data:
          topic: ha_llm_runner/run/doorbell
          payload: RUN
      - wait_for_trigger:
          - trigger: state
            entity_id: sensor.front_door_visitor
        timeout: "00:00:30"
      - action: notify.mobile_app_my_phone
        data:
          title: "Doorbell"
          message: "{{ states('sensor.front_door_visitor') }}"
```

For an Echo announcement, add a second action with your Alexa notify service (e.g. from the Alexa Media Player integration).

### Notes

- The password is stored in plain text in `llm_tasks.yaml`, so use a dedicated view-only camera user. Special characters must be URL-encoded (`@` becomes `%40`).
- Any other camera works the same way: use a `camera.*` entity or its snapshot URL under `entities:`.
- Want the message in German? Add `Answer in German.` to the prompt.
