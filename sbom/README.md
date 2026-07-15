# Sandbox image SBOM

`sandbox-image.cdx.json` is a generated CycloneDX SBOM for the reviewed `code-sandbox-mcp-javascript:1.0.1` image. Regenerate it after every intentional image update:

```powershell
docker scout sbom --format cyclonedx --output sbom\sandbox-image.cdx.json local://code-sandbox-mcp-javascript:1.0.1
```

CI independently generates and uploads a fresh SBOM artifact after building and scanning the image.
