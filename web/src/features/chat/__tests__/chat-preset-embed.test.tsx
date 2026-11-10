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
import { render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'

import { Route } from '@/routes/_authenticated/chat/$chatId'

vi.mock('@/features/chat/hooks/use-chat-presets', () => ({
  useChatPresets: () => ({
    chatPresets: [
      {
        id: '0',
        name: 'Example Chat',
        type: 'web',
        url: 'https://chat.example/?key={key}',
      },
    ],
    serverAddress: 'https://gateway.example',
  }),
}))

vi.mock('@/features/chat/hooks/use-active-chat-key', () => ({
  useActiveChatKey: () => ({ data: 'sk-test' }),
}))

test('a web chat preset opens in an isolated iframe and can open directly in a new tab', () => {
  vi.spyOn(Route, 'useParams').mockReturnValue({ chatId: '0' })
  const ChatRoute = Route.options.component
  if (!ChatRoute) throw new Error('Missing chat route component')
  render(<ChatRoute />)

  const presetUrl = 'https://chat.example/?key=sk-test'
  const frame = screen.getByTitle('Chat preset: Example Chat')
  expect(frame).toHaveAttribute('src', presetUrl)
  expect(frame).toHaveAttribute('sandbox', 'allow-scripts allow-forms')
  const link = screen.getByRole('link', { name: 'Open in new tab' })
  expect(link).toHaveAttribute('href', presetUrl)
  expect(link).toHaveAttribute('target', '_blank')
  expect(link).toHaveAttribute('rel', 'noopener noreferrer')
})
