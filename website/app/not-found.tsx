import { ComingSoon } from "@/components/coming-soon";

export default function NotFound() {
  return (
    <ComingSoon
      eyebrow="404"
      title="This page doesn't exist yet"
      lede="The page you're looking for hasn't been built or has moved. The landing page has everything that's live today."
    />
  );
}
