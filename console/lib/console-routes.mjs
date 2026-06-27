export const consoleHomePath = "/console";

export const activeOrganizationCreateProps = /** @type {const} */ ({
  afterCreateOrganizationUrl: consoleHomePath,
  routing: "hash",
  skipInvitationScreen: true
});
