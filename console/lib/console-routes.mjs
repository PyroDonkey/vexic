export const consoleHomePath = "/console";

export const activeOrganizationCreateProps = /** @type {const} */ ({
  afterCreateOrganizationUrl: `${consoleHomePath}?orgCreated=1`,
  routing: "hash",
  skipInvitationScreen: true
});
