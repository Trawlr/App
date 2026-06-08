# Helper info
This folder has no operational relevance to Trawlr. It contains helper scripts that were created along the way for one reason or another.

## `webhook.py`
Listens for POST requests, used when developing the entity notification engine.

## `New-Certificate.ps1`
Issues/renews a Let's Encrypt cert via Posh-ACME using the Cloudflare DNS-01 plugin.
Requires the domain's zone on Cloudflare and an API token with Zone:DNS:Edit.
Example usage: `.\New-Certificate.ps1 -Domain app.trawlr.net -CFToken "<token>"`