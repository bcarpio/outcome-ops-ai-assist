import { useState } from 'react'
import { Bot, ShieldCheck } from 'lucide-react'

const STORAGE_KEY = 'ai-disclosure-accepted'

export default function AiDisclosureModal() {
  const [accepted, setAccepted] = useState(() => {
    return localStorage.getItem(STORAGE_KEY) === 'true'
  })

  // Feature toggle via Vite env var (defaults to enabled)
  const enabled = import.meta.env.VITE_AI_DISCLOSURE_ENABLED !== 'false'

  if (!enabled || accepted) return null

  const handleAccept = () => {
    localStorage.setItem(STORAGE_KEY, 'true')
    setAccepted(true)
  }

  // Declining the AI-use notice means the user may not proceed. Route through
  // the normal /api/logout, which clears the ALB session cookies AND redirects
  // through Azure AD's end_session endpoint -- clearing cookies alone would let
  // the ALB silently re-authenticate them on the next request.
  const handleDecline = () => {
    window.location.href = '/api/logout'
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4">
        <div className="flex flex-col items-center p-6 pt-8">
          <div className="flex items-center justify-center w-14 h-14 rounded-full bg-blue-100 mb-4">
            <Bot size={28} className="text-blue-600" />
          </div>
          <h2 className="text-xl font-semibold text-gray-900 mb-1">AI-Powered Assistant</h2>
          <p className="text-sm text-gray-500 mb-4">Important notice before you begin</p>
        </div>

        <div className="px-6 pb-4 space-y-3 text-sm text-gray-700">
          <p>
            This application uses <strong>artificial intelligence</strong> to generate responses,
            code, and recommendations. You are interacting with an AI system, not a human.
          </p>
          <p>
            Please be aware of the following:
          </p>
          <ul className="list-disc pl-5 space-y-1.5">
            <li>AI-generated content may contain errors and should be reviewed before use</li>
            <li>Do not share sensitive personal information beyond what is required for your task</li>
            <li>Interactions are logged for audit and security purposes</li>
            <li>AI outputs do not constitute professional advice</li>
          </ul>
        </div>

        <div className="flex items-center gap-2 px-6 pb-6 pt-2">
          <ShieldCheck size={16} className="text-green-600 flex-shrink-0" />
          <p className="text-xs text-gray-500">
            Your organization's data governance and usage policies apply to all interactions.
          </p>
        </div>

        <div className="border-t px-6 py-4 flex gap-3">
          <button
            onClick={handleDecline}
            className="flex-1 px-4 py-2.5 bg-white text-gray-700 text-sm font-medium rounded-lg border border-gray-300 hover:bg-gray-50 transition-colors focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2"
          >
            Not Now
          </button>
          <button
            onClick={handleAccept}
            className="flex-1 px-4 py-2.5 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2"
          >
            I Accept
          </button>
        </div>
      </div>
    </div>
  )
}
