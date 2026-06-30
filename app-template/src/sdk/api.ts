/**
 * AIHub App SDK - Data Fetching
 * Provides utilities for fetching data from the platform API.
 */

const PLATFORM_API = '/api'

export async function fetchData<T>(endpoint: string): Promise<T> {
  const response = await fetch(`${PLATFORM_API}${endpoint}`, {
    credentials: 'include',
  })
  if (!response.ok) {
    throw new Error(`API error: ${response.status}`)
  }
  return response.json()
}
