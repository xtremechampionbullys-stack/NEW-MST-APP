'use client'

import Link from 'next/link'
import { FormEvent, useState } from 'react'

export default function LoginPage() {
  const [message, setMessage] = useState('')
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setMessage('Secure account sign-in will be enabled when the MarketShark identity provider is connected. You can open the research workspace now.')
  }

  return (
    <main className="auth-page">
      <div className="public-orb public-orb-one" aria-hidden />
      <div className="public-orb public-orb-two" aria-hidden />
      <Link className="public-brand auth-brand" href="/"><span className="public-mark"><svg viewBox="0 0 24 24" aria-hidden><path d="M3 15c4-1 6-4 8-8 1.6 3.2 3.5 5 6.1 6L22 9l-3.2 8C15.2 20 8.7 20.3 5 18z" /></svg></span><span>MarketShark <small>TRADER</small></span></Link>
      <section className="auth-card">
        <div className="auth-copy"><div className="public-eyebrow"><span /> MARKETSHARK PORTAL</div><h1>Welcome back.</h1><p>Your research workspace, strategy history, saved views, and partner profile—ready when you are.</p></div>
        <form onSubmit={submit}>
          <label>Email address<input required type="email" autoComplete="email" placeholder="you@example.com" /></label>
          <label>Password<input required type="password" autoComplete="current-password" placeholder="••••••••••" /></label>
          <div className="auth-form-row"><label className="remember"><input type="checkbox" /> Remember this device</label><button type="button">Forgot password?</button></div>
          <button className="public-button auth-submit" type="submit">Continue securely <span>→</span></button>
        </form>
        {message && <p className="auth-status" role="status">{message}</p>}
        <div className="auth-divider"><span>or</span></div>
        <Link className="auth-workspace" href="/?workspace=1">Explore the workspace <span>↗</span></Link>
        <p className="auth-legal">Account authentication and partner enrollment will be enabled at launch. No credentials are stored by this preview.</p>
      </section>
      <p className="auth-back"><Link href="/">← Back to MarketShark</Link></p>
    </main>
  )
}
