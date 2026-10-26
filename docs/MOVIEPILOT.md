# MoviePilot integration

The included adapter is `moviepilot_plugin/plugins.v3/csfreborn`.

It listens for `TransferComplete`, extracts the final video path and media
metadata, then calls:

```text
POST {service_url}/v1/process
```

## Installation options

For a custom plugin repository, publish `moviepilot_plugin` to a GitHub
repository while preserving this layout:

```text
plugins.v3/csfreborn/__init__.py
package.v3.json
```

Then add the repository's `main` branch URL to MoviePilot's `PLUGIN_MARKET`.

For a manual test, copy the plugin directory into MoviePilot's plugin source
directory, restart MoviePilot, and inspect the MoviePilot log for plugin loading
errors. The exact source directory depends on the MoviePilot version and
installation method.

## Configuration

In the MoviePilot plugin page:

1. Enable the plugin.
2. Set `service_url`.
3. Paste the same token used by `SF_API_TOKEN`.
4. Choose `zh-Hans` and/or `zh-Hant`.
5. Keep overwrite off unless you explicitly want replacement.

Use the plugin's `/health` endpoint to check the service.

## Failure behavior

Provider failures are returned in the job result and displayed in plugin
history. A failed subtitle search does not roll back the media transfer and
does not crash MoviePilot.

