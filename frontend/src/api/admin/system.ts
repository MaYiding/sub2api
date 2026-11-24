/**
 * System API endpoints for admin operations
 */

import { apiClient } from '../client'

// Update check, in-place update, rollback and restart are deliberately not
// offered: the backend no longer registers those routes, and this deployment
// is upgraded by replacing the image.

/**
 * Get current version
 */
export async function getVersion(): Promise<{ version: string }> {
  const { data } = await apiClient.get<{ version: string }>('/admin/system/version')
  return data
}

export const systemAPI = {
  getVersion
}

export default systemAPI
