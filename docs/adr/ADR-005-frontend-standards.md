# ADR-005: Frontend Development Standards

## Status: Accepted

## Context

OutcomeOps AI Assist frontend is a React-based single-page application (SPA) built with Vite, TypeScript, Tailwind CSS, and React Router. Establishing consistent patterns ensures maintainability, security, and user experience consistency across the application.

## Decision

### 1. Technology Stack

- Framework: React 18+
- Language: TypeScript
- Bundler: Vite
- Styling: Tailwind CSS
- Routing: React Router v6+
- Icons: lucide-react
- Package Manager: npm

### 2. Project Structure

```
src/
├── main.tsx                    # Application entry point with Router
├── App.tsx                     # Main App component
├── App.css                     # Global styles
├── tailwind.css                # Tailwind configuration
├── index.css                   # Global CSS
│
├── api.ts                      # API client (apiFetch helper)
├── GlobalGuard.tsx             # Global context for modals and state
├── lockoutHandler.ts           # Account lockout handler
├── moderationHandler.ts        # Content moderation handler
├── creditHandler.ts            # Insufficient credits handler
│
├── components/                 # Reusable UI components
│   ├── CharacterBanner.tsx
│   ├── AccountLockoutLegend.tsx
│   ├── ModerationFailureModal.tsx
│   ├── InsufficientCreditsModal.tsx
│   └── UnifiedSidebarLayout.tsx
│
├── hooks/                      # Custom React hooks
├── utils/                      # Utility functions
├── lib/                        # Third-party integrations
├── constants/                  # Application constants
├── data/                       # Static data files
└── assets/                     # Images, fonts, etc.
```

### 3. API Client (apiFetch)

All API calls go through the apiFetch helper. Never use fetch directly.

**apiFetch helper:**

```typescript
// src/api.ts
import { showLockoutModal } from './lockoutHandler';
import { showInsufficientCreditsModal } from './creditHandler';

const getApiBaseUrl = () => {
  if (import.meta.env.DEV) {
    return 'https://api.dev.myfantasy.ai';
  }
  if (window.location.hostname.includes('dev.myfantasy.ai')) {
    return 'https://api.dev.myfantasy.ai';
  }
  return 'https://api.prd.myfantasy.ai';
};

const API_BASE_URL = getApiBaseUrl();

export async function apiFetch(path: string, options: RequestInit = {}) {
  const url = `${API_BASE_URL}${path}`;
  const token = localStorage.getItem("fantacy_token");

  const isFormData = options.body instanceof FormData;

  const headers = {
    ...(isFormData ? {} : { "Content-Type": "application/json" }),
    ...(options.headers || {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };

  const res = await fetch(url, {
    ...options,
    headers,
  });

  // Handle account lockout (403 with "Account disabled")
  if (res.status === 403) {
    try {
      const data = await res.clone().json();
      if (data?.error?.includes("Account disabled")) {
        showLockoutModal();
        return { __lockout: true };
      }
    } catch (e) {
      console.error("Error checking lockout status:", e);
    }
  }

  // Handle insufficient credits (402)
  if (res.status === 402) {
    showInsufficientCreditsModal();
    return { __insufficient_credits: true };
  }

  return res;
}
```

**Usage pattern:**

```typescript
// In a component or hook
const response = await apiFetch('/api/profile', {
  method: 'GET'
});

if (response.__lockout) {
  // User is locked out, modal is already shown
  return;
}

if (response.__insufficient_credits) {
  // User has insufficient credits, modal is already shown
  return;
}

const data = await response.json();
```

**apiFetch features:**
- Automatically adds JWT token from localStorage (key: fantacy_token)
- Handles FormData without setting Content-Type
- Detects account lockout (403 with "Account disabled" error) and shows modal
- Detects insufficient credits (402) and shows modal
- Returns raw Response object for flexibility

### 4. Global Guard (GlobalGuard.tsx)

GlobalGuard is a context provider that wraps the entire application. It manages global modals and state that need to be accessible from anywhere in the app.

**Features:**
- Account lockout modal
- Content moderation failure modal
- Insufficient credits modal

**Usage in main.tsx:**

```typescript
import { GlobalGuard } from './GlobalGuard.tsx';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <GlobalGuard>
      <Router>
        {/* All routes wrapped here */}
      </Router>
    </GlobalGuard>
  </React.StrictMode>
);
```

**Accessing lockout state from components:**

```typescript
import { useLockout } from './GlobalGuard';

function MyComponent() {
  const { showLockout } = useLockout();

  if (showLockout) {
    return <div>Account is locked</div>;
  }

  return <div>Component content</div>;
}
```

### 5. Modal Handlers

Three handler files manage global modals triggered from anywhere in the app, including from API responses.

**Lockout Handler (lockoutHandler.ts):**

```typescript
// src/lockoutHandler.ts
let callback: (() => void) | null = null;

export function registerLockoutCallback(fn: () => void) {
  callback = fn;
}

export function showLockoutModal() {
  if (callback) callback();
}
```

Called automatically by apiFetch when detecting account lockout.

**Moderation Handler (moderationHandler.ts):**

```typescript
// src/moderationHandler.ts
type ModerationReason =
  | string
  | {
      flagged_reason: string;
      flagged_word: string;
    };

let callback: ((reason: ModerationReason) => void) | null = null;

export function registerModerationCallback(fn: (reason: ModerationReason) => void) {
  callback = fn;
}

export function showModerationModal(reason: ModerationReason) {
  if (callback) callback(reason);
}
```

Usage - call when content is rejected by moderation system:

```typescript
import { showModerationModal } from './moderationHandler';

// Show with string reason
showModerationModal("Content violates community standards");

// Show with detailed reason
showModerationModal({
  flagged_reason: "Inappropriate content",
  flagged_word: "banned_word"
});
```

**Credit Handler (creditHandler.ts):**

```typescript
// src/creditHandler.ts
let creditCallback: () => void;

export function registerCreditCallback(cb: () => void) {
  creditCallback = cb;
}

export function showInsufficientCreditsModal() {
  if (creditCallback) creditCallback();
}
```

Called automatically by apiFetch when API returns 402 status.

### 6. Protected Routes

Use ProtectedRoute component to guard routes that require authentication.

**Implementation:**

```typescript
function ProtectedRoute({ children }: { children: JSX.Element }) {
  const hostname = window.location.hostname;
  const user = localStorage.getItem('user');

  // In dev mode (localhost), allow access without login
  if (hostname === "localhost") {
    return children;
  }

  // In production, enforce authentication
  return user ? children : <Navigate to="/login" replace />;
}
```

**Usage in routes:**

```typescript
<Route path="/dashboard" element={<ProtectedRoute><Dashboard /></ProtectedRoute>} />
<Route path="/profile" element={<ProtectedRoute><Profile /></ProtectedRoute>} />
```

### 7. React Router Structure

Define all routes in main.tsx using React Router v6:

```typescript
<Routes>
  {/* Public routes */}
  <Route path="/login" element={<Login />} />
  <Route path="/signup" element={<SignupRedirect />} />
  <Route path="/landing" element={<Landing />} />
  <Route path="/privacy" element={<PrivacyPolicy />} />

  {/* Protected routes */}
  <Route path="/dashboard" element={<ProtectedRoute><Dashboard /></ProtectedRoute>} />
  <Route path="/profile" element={<ProtectedRoute><Profile /></ProtectedRoute>} />

  {/* Admin routes */}
  <Route path="/admin/users" element={<ProtectedRoute><AdminUsers /></ProtectedRoute>} />

  {/* Fallback */}
  <Route path="*" element={<Navigate to="/landing" replace />} />
</Routes>
```

### 8. Styling with Tailwind CSS

Use Tailwind CSS for all styling. Never use inline styles or CSS modules unless absolutely necessary.

**Tailwind configuration:** tailwind.config.js

**Tailwind CSS file:** src/tailwind.css

**Usage:**

```typescript
// Good - Tailwind classes
<div className="bg-blue-500 text-white px-4 py-2 rounded-lg hover:bg-blue-600">
  Click me
</div>

// Avoid - Inline styles
<div style={{ backgroundColor: 'blue', color: 'white' }}>
  Click me
</div>
```

**Layout components:** Use Tailwind grid, flexbox, and responsive utilities

```typescript
// Responsive layout
<div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
  {items.map(item => <Card key={item.id} {...item} />)}
</div>

// Responsive text size
<h1 className="text-2xl md:text-3xl lg:text-4xl">Heading</h1>
```

### 9. Component Organization

**Reusable components go in src/components/:**

```typescript
// src/components/Button.tsx
interface ButtonProps {
  variant?: 'primary' | 'secondary';
  children: React.ReactNode;
  onClick?: () => void;
}

export default function Button({
  variant = 'primary',
  children,
  onClick
}: ButtonProps) {
  const classes = variant === 'primary'
    ? 'bg-blue-500 text-white hover:bg-blue-600'
    : 'bg-gray-200 text-gray-800 hover:bg-gray-300';

  return (
    <button
      onClick={onClick}
      className={`px-4 py-2 rounded-lg transition ${classes}`}
    >
      {children}
    </button>
  );
}
```

**Page components go in src/ (top level):**

```
src/
├── Dashboard.tsx
├── Profile.tsx
├── Login.tsx
├── CreateCharacter.tsx
└── ...
```

### 10. TypeScript Usage

Always use TypeScript for type safety.

```typescript
// Good - Typed interface
interface User {
  id: string;
  email: string;
  role: 'user' | 'creator' | 'admin';
}

function displayUser(user: User) {
  return <div>{user.email}</div>;
}

// Good - Typed component props
interface CardProps {
  title: string;
  children: React.ReactNode;
  onClick?: (id: string) => void;
}

export function Card({ title, children, onClick }: CardProps) {
  return <div>{title}{children}</div>;
}

// Avoid - any type
function displayUser(user: any) {
  return <div>{user.email}</div>;
}
```

### 11. Local Storage Key

JWT token is stored with key: fantacy_token

User info is stored with key: user (JSON stringified)

```typescript
// Get token
const token = localStorage.getItem("fantacy_token");

// Get user
const user = JSON.parse(localStorage.getItem('user') || '{}');

// Store token after login
localStorage.setItem("fantacy_token", token);

// Store user after login
localStorage.setItem('user', JSON.stringify(userData));

// Clear on logout
localStorage.removeItem("fantacy_token");
localStorage.removeItem('user');
```

### 12. Environment Variables

Accessed via import.meta.env:

```typescript
if (import.meta.env.DEV) {
  // Development mode
}

if (import.meta.env.PROD) {
  // Production mode
}
```

### 13. Vite Configuration

vite.config.ts defines build settings and aliases.

Development server runs at http://localhost:5173 by default.

### 14. Development Server

Run development server:

```bash
npm run dev
```

The app connects to https://api.dev.myfantasy.ai by default.

### 15. Build Process

Build for production:

```bash
npm run build
```

Creates optimized bundle in dist/ directory.

Preview production build locally:

```bash
npm run preview
```

## Consequences

### Positive
- Consistent API usage through apiFetch helper
- Global state management for modals via context
- Type safety with TypeScript reduces bugs
- Tailwind CSS ensures consistent styling
- Protected routes prevent unauthorized access
- Clear component organization for scaling

### Tradeoffs
- TypeScript adds initial setup complexity (worth it for safety)
- Tailwind CSS utility classes can look verbose (very flexible)
- Global handlers require modal callback registration (simple pattern)

## Implementation

### Starting today
1. Use apiFetch for all API calls (never raw fetch)
2. Call handlers (showLockoutModal, showModerationModal, etc.) when needed
3. Use TypeScript for all new components
4. Use Tailwind CSS for all styling
5. Wrap all routes that need auth with ProtectedRoute

### Next phases
1. Create component library with common patterns
2. Add error boundary for better error handling
3. Consider state management library if needed (currently simple with context)

## Related ADRs

- ADR-004: Lambda Handler Standards (API contract)
- ADR-002: Development Workflow Standards

## References

- React Documentation: https://react.dev
- React Router: https://reactrouter.com
- Vite: https://vitejs.dev
- Tailwind CSS: https://tailwindcss.com
- TypeScript: https://www.typescriptlang.org
- lucide-react: https://lucide.dev

Version History:
- v1.0 (2025-01-02): Initial frontend standards for OutcomeOps AI Assist React SPA
