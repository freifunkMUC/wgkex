# Blacklist Feature

The blacklist feature allows you to reject specific WireGuard keys from connecting to the broker. This is useful for blocking abusive clients.

## Configuration

To enable the blacklist feature, add the `blacklist_file` option to your `wgkex.yaml` configuration:

```yaml
# Optional: Path to blacklist file containing keys that should be rejected
blacklist_file: /etc/wgkex-blacklist.yaml
```

## Blacklist File Format

The blacklist file uses YAML format and supports multiple styles:

### Simple list of keys
```yaml
- Key1
- Key2
- Key3
```

### Keys with reasons
```yaml
- o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=:
    reason: "Abusive behavior"
- TszFS3oFRdhsJP3K0VOlklGMGYZy+oFCtlaghXJqW2g=:
    reason: "Spam"
```

### Mixed format
```yaml
- o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=
- TszFS3oFRdhsJP3K0VOlklGMGYZy+oFCtlaghXJqW2g=:
    reason: "Abuse"
- AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
```

## Behavior

- When a blacklisted key tries to connect, the broker will return a 400 error
- If a reason is provided in the blacklist, it will be included in the error response
- The blacklist file is automatically reloaded when it changes (checked every 10 seconds)
- If the blacklist file doesn't exist, no keys are blocked
- Blacklisted keys are **not** published to MQTT, preventing them from reaching workers

## Error Response

When a blacklisted key is rejected, the broker returns:

```json
{
  "error": {
    "message": "An internal error has occurred. Please try again later."
  }
}
```

The actual error (including the reason if provided) is logged on the broker for administrators to review.

## Example Usage

1. Create a blacklist file:
```bash
cat > /etc/wgkex-blacklist.yaml << 'EOF'
- o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=:
    reason: "Abusive behavior"
EOF
```

2. Configure wgkex to use it:
```yaml
blacklist_file: /etc/wgkex-blacklist.yaml
```

3. Restart the broker or wait for auto-reload

4. Add more keys as needed - changes are picked up automatically:
```bash
echo '- TszFS3oFRdhsJP3K0VOlklGMGYZy+oFCtlaghXJqW2g=:' >> /etc/wgkex-blacklist.yaml
echo '    reason: "Spam"' >> /etc/wgkex-blacklist.yaml
```

## Testing

You can test if a key is blacklisted by attempting a key exchange:

```bash
curl -X POST http://127.0.0.1:5000/api/v1/wg/key/exchange \
  -H "Content-Type: application/json" \
  -d '{"domain": "your_domain", "public_key": "o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg="}'
```

If the key is blacklisted, you'll receive a 400 error response.
