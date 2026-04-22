# Quadlet Units

Podman Quadlet files allow VirtValidate containers to run as
systemd services — the proper Podman-native production deployment
model. No podman-compose required in production.

## Usage
Copy *.container files to ~/.config/containers/systemd/
Then: systemctl --user daemon-reload
      systemctl --user start virtvalidate-backend

## Files
- virtvalidate-postgres.container
- virtvalidate-ollama.container  
- virtvalidate-backend.container
- virtvalidate-frontend.container
- virtvalidate.network
