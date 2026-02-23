#!/bin/sh
# Runtime config injection
# Replaces placeholder in built JS files with actual environment variable values

# Replace streaming URL placeholder
if [ -n "$VITE_STREAMING_URL" ]; then
  find /usr/share/nginx/html -type f -name "*.js" -exec sed -i "s|__STREAMING_URL_PLACEHOLDER__|$VITE_STREAMING_URL|g" {} +
fi

# Start nginx
exec nginx -g "daemon off;"
