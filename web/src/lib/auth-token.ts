'use client'

import { createClient } from '@/lib/supabase/client'
import { useUserStore } from '@/stores'

const isLocal = process.env.NEXT_PUBLIC_MODE === 'local'

export async function refreshAccessToken(): Promise<string | null> {
  if (isLocal) {
    useUserStore.getState().setAccessToken('local')
    return 'local'
  }

  const supabase = createClient()
  const { data, error } = await supabase.auth.refreshSession()
  if (error || !data.session?.access_token) {
    const { data: current } = await supabase.auth.getSession()
    const token = current.session?.access_token ?? null
    useUserStore.getState().setAccessToken(token)
    return token
  }

  const token = data.session.access_token
  useUserStore.getState().setAccessToken(token)
  return token
}
