// Starts a dashboard test already signed in (issue #64), as if the operator
// had logged in earlier in this tab. Tests of the sign-in itself are in
// auth.spec.js.
export const TEST_SESSION = { token: 'e2e-token', operator: 'e2e-commander', role: 'FLIGHT_COMMANDER' };

export async function signIn(page, session = TEST_SESSION) {
  await page.addInitScript((s) => sessionStorage.setItem('tara_gcs_session', JSON.stringify(s)), session);
}
