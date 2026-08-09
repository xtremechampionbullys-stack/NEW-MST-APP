import type { Metadata } from 'next'
import { ThemeScript } from '@/components/Settings'
import './globals.css'

export const metadata: Metadata = {
  title: 'MarketShark Trader — Market Intelligence, Refined',
  description:
    'A focused research workspace for options strategy design, market intelligence, and disciplined trade planning.',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // Liquid glass is the server-rendered default. ThemeScript may replace it
  // synchronously from storage before hydration; suppressing this one expected
  // attribute difference avoids a hydration warning on every route.
  return (
    <html lang="en" data-theme="glass" suppressHydrationWarning>
      <head>
        <ThemeScript />
      </head>
      <body>{children}</body>
    </html>
  )
}
