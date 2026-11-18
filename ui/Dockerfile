# Multi-stage build for OutcomeOps Chat UI with streaming proxy
#
# Stage 1: Build React app
# Stage 2: Build Node.js server
# Stage 3: Production runtime

# ============================================================================
# Stage 1: Build React frontend
# ============================================================================
FROM node:22-alpine AS frontend-builder

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci

# Copy source code
COPY . .

# Build for production
RUN npm run build

# ============================================================================
# Stage 2: Install server dependencies
# ============================================================================
FROM node:22-alpine AS server-builder

WORKDIR /app/server

# Copy server package files
COPY server/package*.json ./

# Copy lock file and install production dependencies only
COPY server/package-lock.json ./
RUN npm ci --omit=dev

# ============================================================================
# Stage 3: Production runtime
# ============================================================================
FROM node:22-alpine AS production

WORKDIR /app

# Create non-root user for security
RUN addgroup -g 1001 -S nodejs && \
    adduser -S nodejs -u 1001

# Copy server code and dependencies. Ship every top-level .js in server/
# so factored-out modules (middleware.js) aren't left behind
# on a per-file COPY line -- an ERR_MODULE_NOT_FOUND at container start
# is the exact failure that surfaced when middleware.js was extracted.
COPY --from=server-builder /app/server/node_modules ./server/node_modules
COPY server/*.js ./server/
COPY server/package.json ./server/

# Copy built frontend assets
COPY --from=frontend-builder /app/dist ./static

# Set ownership
RUN chown -R nodejs:nodejs /app

# Switch to non-root user
USER nodejs

# Environment variables
ENV NODE_ENV=production
ENV PORT=3000
ENV STATIC_DIR=/app/static

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://localhost:3000/health || exit 1

EXPOSE 3000

# Start the server
CMD ["node", "server/index.js"]
