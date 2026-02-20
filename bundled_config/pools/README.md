# Pool Configurations

This directory contains individual pool configuration files. Each YAML file defines one mining pool endpoint.

## 🚀 First Time Setup

On first run, example pool configs are deployed as `.yaml.example` files. To activate:

```bash
cd /config/pools
cp solopool-dgb-eu1.yaml.example solopool-dgb-eu1.yaml
```

Then restart the container to load the pool.

## Configuration Format

```yaml
# Which driver handles this pool
driver: solopool

# Display information
display_name: "Solopool.org DGB (EU1)"
description: "DigiByte solo mining - European server"

# Connection details
url: eu1.solopool.org
port: 8004

# Pool characteristics
coin: DGB
region: EU
mining_model: solo  # solo or pool
fee_percent: 0.0

# Capabilities (what the pool API supports)
requires_auth: false
supports_shares: false
supports_earnings: false
supports_balance: false
```

## Drivers

The `driver` field must match a driver in `/config/drivers/`:
- `solopool` → uses `solopool_driver.py`
- `braiins` → uses `braiins_driver.py`
- `mmfp` → uses `mmfp_driver.py`

## User Secrets

- Wallet addresses are entered in the Pools UI.
- Braiins API token is stored in the Braiins pool config file (`/config/pools/braiins-*.yaml`) under `api_token`.
- Other global credentials remain in `/config/config.yaml` where applicable.

## Adding Pools

1. Create a new `.yaml` file with your pool config
2. Restart the container
3. Pool appears in UI automatically

## Editing Pools

Edit the YAML file directly and restart the container.
