import "./index.css";
import {Composition} from "remotion";
import {
  CourseTeaser,
  ExplainerCarousel,
  LinkedInShort,
  PolicyBriefing,
  ProposalWalkthrough,
  TalkingHeadClip,
} from "./Composition";

const common = {
  durationInFrames: 180,
  fps: 30,
  width: 1280,
  height: 720,
  defaultProps: {
    title: "Production draft",
    format: "linkedin_short",
    script: {},
    asset_plan: {},
    edit_plan: {},
  },
};

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition id="linkedin_short" component={LinkedInShort} {...common} />
      <Composition id="explainer_carousel" component={ExplainerCarousel} {...common} />
      <Composition id="talking_head_clip" component={TalkingHeadClip} {...common} />
      <Composition id="policy_briefing" component={PolicyBriefing} {...common} />
      <Composition id="course_teaser" component={CourseTeaser} {...common} />
      <Composition id="proposal_walkthrough" component={ProposalWalkthrough} {...common} />
    </>
  );
};
