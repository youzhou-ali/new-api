/*
Copyright (C) 2023-2026 QuantumNous

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as
published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

For commercial licensing, please contact support@quantumnous.com
*/
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'

import { ModelCard } from '../components/model-card'
import { ModelDetailsContent } from '../components/model-details'
import type { PricingModel } from '../types'

// Canvas charts are a browser-only dependency; search details must not render them.
vi.mock('@visactor/react-vchart', () => ({ VChart: () => null }))

const model: PricingModel = {
  id: 1,
  model_name: 'tavily-search',
  quota_type: 1,
  model_ratio: 0,
  completion_ratio: 0,
  enable_groups: ['default'],
  supported_endpoint_types: ['search'],
  billing_mode: 'tiered_expr',
  billing_expr: 'tier("request", fixed(0.01))',
}
let client: QueryClient

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['status'], { server_address: 'https://relay.example' })
})
afterEach(() => {
  cleanup()
  client.clear()
})

test('search details show request parameters and copy working HTTP samples without chat SDK calls', async () => {
  const user = userEvent.setup()
  render(
    <QueryClientProvider client={client}>
      <ModelDetailsContent
        model={model}
        groupRatio={{ default: 1 }}
        usableGroup={{ default: { desc: 'Default', ratio: 1 } }}
        endpointMap={{ search: { path: '/v1/search', method: 'POST' } }}
        autoGroups={[]}
        priceRate={1}
        usdExchangeRate={1}
        tokenUnit='M'
      />
    </QueryClientProvider>
  )
  expect(
    screen.queryByRole('tab', { name: 'Performance' })
  ).not.toBeInTheDocument()
  expect(screen.queryByText('TPS')).not.toBeInTheDocument()
  await user.click(screen.getByRole('tab', { name: 'API' }))
  expect(screen.getByText('query')).toBeVisible()
  expect(screen.getByText('max_results')).toBeVisible()
  expect(screen.getByText('freshness')).toBeVisible()
  expect(screen.getByText('1 ~ 20')).toBeVisible()
  expect(screen.queryByText('temperature')).not.toBeInTheDocument()
  expect(screen.queryByText('Rate limits')).not.toBeInTheDocument()
  for (const language of ['cURL', 'Python', 'TypeScript', 'JavaScript']) {
    await user.click(screen.getByRole('tab', { name: language }))
    await user.click(screen.getByRole('button', { name: /Copy code|Copied!/ }))
    const sample = await navigator.clipboard.readText()
    expect(sample).toContain('https://relay.example/v1/search')
    expect(sample).toContain('tavily-search')
    expect(sample).toContain('"query"')
    expect(sample).toContain('"max_results": 5')
    expect(sample).not.toContain('chat.completions')
    expect(sample).not.toContain('messages')
    expect(sample).not.toContain('stream')
  }
})

test('search cards retain request pricing and details without token throughput metrics', () => {
  render(<ModelCard model={model} onClick={() => undefined} />)
  expect(screen.getByRole('button', { name: 'Details' })).toBeVisible()
  expect(screen.getByText('search')).toBeVisible()
  expect(screen.queryByTitle('Throughput')).not.toBeInTheDocument()
  expect(screen.getByText('/ request')).toBeVisible()
})
