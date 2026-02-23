/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_STREAMING_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
