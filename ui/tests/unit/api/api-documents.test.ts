// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  getDocumentUploadUrl,
  uploadDocumentToS3,
  listDocuments,
  deleteDocument,
} from '../../../src/lib/api'
import { createWorkspaceDocument, createDocumentUploadUrlResponse, createMockFetchResponse } from '../../helpers/mocks'

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('getDocumentUploadUrl', () => {
  it('returns presigned upload URL', async () => {
    const response = createDocumentUploadUrlResponse()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(response))

    const result = await getDocumentUploadUrl('ws-1', 'file.pdf', 'application/pdf', 'docs/')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/documents/upload',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          filename: 'file.pdf',
          content_type: 'application/pdf',
          folder_path: 'docs/',
        }),
      })
    )
    expect(result.upload_url).toBe('https://s3.amazonaws.com/bucket/key?presigned')
    expect(result.s3_key).toBe('workspaces/ws-1/docs/file.pdf')
    expect(result.bucket).toBe('test-bucket')
    expect(result.expires_in).toBe(3600)
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Forbidden' }, false, 403))
    await expect(getDocumentUploadUrl('ws-1', 'file.pdf')).rejects.toThrow('Forbidden')
  })

  it('throws on empty response', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve(''),
    })
    await expect(getDocumentUploadUrl('ws-1', 'file.pdf')).rejects.toThrow('Empty response from server')
  })
})

function createMockXhr(statusCode = 200, statusText = 'OK') {
  const instance = {
    upload: { addEventListener: vi.fn() },
    addEventListener: vi.fn(),
    open: vi.fn(),
    setRequestHeader: vi.fn(),
    send: vi.fn(),
    status: statusCode,
    statusText,
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const MockXHR = function (this: any) {
    Object.assign(this, instance)
    this.upload = { addEventListener: vi.fn() }
    this.addEventListener = vi.fn()
    // Copy refs so tests can access them
    Object.assign(instance, {
      upload: this.upload,
      addEventListener: this.addEventListener,
    })
  } as unknown as typeof XMLHttpRequest
  vi.stubGlobal('XMLHttpRequest', MockXHR)
  return instance
}

function getEventHandler(mock: ReturnType<typeof vi.fn>, eventName: string) {
  const call = mock.mock.calls.find((c: [string, () => void]) => c[0] === eventName)
  return call?.[1]
}

describe('uploadDocumentToS3', () => {
  it('uploads file via XMLHttpRequest', async () => {
    const xhr = createMockXhr(200)

    const file = new File(['content'], 'test.pdf', { type: 'application/pdf' })
    const progressFn = vi.fn()
    const promise = uploadDocumentToS3('https://s3.example.com/upload', file, progressFn)

    const loadHandler = getEventHandler(xhr.addEventListener, 'load')
    loadHandler()

    await promise

    expect(xhr.open).toHaveBeenCalledWith('PUT', 'https://s3.example.com/upload')
    expect(xhr.setRequestHeader).toHaveBeenCalledWith('Content-Type', 'application/pdf')
    expect(xhr.send).toHaveBeenCalledWith(file)
  })

  it('tracks upload progress', async () => {
    const xhr = createMockXhr(200)

    const file = new File(['content'], 'test.pdf', { type: 'application/pdf' })
    const progressFn = vi.fn()
    const promise = uploadDocumentToS3('https://s3.example.com/upload', file, progressFn)

    const progressHandler = getEventHandler(xhr.upload.addEventListener, 'progress')
    progressHandler({ lengthComputable: true, loaded: 50, total: 100 })
    expect(progressFn).toHaveBeenCalledWith(50)

    const loadHandler = getEventHandler(xhr.addEventListener, 'load')
    loadHandler()

    await promise
  })

  it('rejects on upload error', async () => {
    const xhr = createMockXhr(500, 'Internal Server Error')

    const file = new File(['content'], 'test.pdf', { type: 'application/pdf' })
    const promise = uploadDocumentToS3('https://s3.example.com/upload', file)

    const loadHandler = getEventHandler(xhr.addEventListener, 'load')
    loadHandler()

    await expect(promise).rejects.toThrow('Upload failed: 500 Internal Server Error')
  })

  it('rejects on network error', async () => {
    const xhr = createMockXhr()

    const file = new File(['content'], 'test.pdf', { type: 'application/pdf' })
    const promise = uploadDocumentToS3('https://s3.example.com/upload', file)

    const errorHandler = getEventHandler(xhr.addEventListener, 'error')
    errorHandler()

    await expect(promise).rejects.toThrow('Upload failed: Network error')
  })

  it('rejects on abort', async () => {
    const xhr = createMockXhr()

    const file = new File(['content'], 'test.pdf', { type: 'application/pdf' })
    const promise = uploadDocumentToS3('https://s3.example.com/upload', file)

    const abortHandler = getEventHandler(xhr.addEventListener, 'abort')
    abortHandler()

    await expect(promise).rejects.toThrow('Upload aborted')
  })
})

describe('listDocuments', () => {
  it('returns documents', async () => {
    const doc = createWorkspaceDocument()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ documents: [doc] }))

    const result = await listDocuments('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/documents')
    expect(result).toHaveLength(1)
    expect(result[0].filename).toBe('readme.md')
  })

  it('passes prefix query parameter', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ documents: [] }))
    await listDocuments('ws-1', 'docs/')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/documents?prefix=docs%2F')
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Forbidden' }, false, 403))
    await expect(listDocuments('ws-1')).rejects.toThrow('Forbidden')
  })
})

describe('deleteDocument', () => {
  it('deletes a document', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    await deleteDocument('ws-1', 'docs/readme.md')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/documents/docs%2Freadme.md',
      expect.objectContaining({ method: 'DELETE' })
    )
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Not found' }, false, 404))
    await expect(deleteDocument('ws-1', 'missing.md')).rejects.toThrow('Not found')
  })
})
