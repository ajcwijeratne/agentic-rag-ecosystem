import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from "remotion";

type Scene = {
  scene_id?: string;
  title?: string;
  text?: string;
  asset_id?: string;
  thumbnail_path?: string;
  needs_generation?: boolean;
};

export type ProductionProps = {
  production_id?: string;
  title?: string;
  project?: string;
  format?: string;
  owner?: string;
  script?: Record<string, unknown>;
  asset_plan?: Record<string, unknown>;
  edit_plan?: Record<string, unknown>;
  lines?: string[];
  captions?: string[];
  scenes?: Scene[];
  linked_assets?: string[];
  brand?: Record<string, string>;
};

const palette = {
  pine: "#1f4d3f",
  ink: "#14231f",
  gold: "#b88724",
  paper: "#f7f4ee",
  mist: "#dfe8e1",
  slate: "#475569",
  teal: "#2f6f83",
};

const label: Record<string, string> = {
  linkedin_short: "LinkedIn short",
  explainer_carousel: "Explainer carousel",
  talking_head_clip: "Talking-head clip",
  policy_briefing: "Policy briefing",
  course_teaser: "Course teaser",
  proposal_walkthrough: "Proposal walkthrough",
};

const flattenText = (value: unknown): string[] => {
  if (!value) return [];
  if (typeof value === "string") return value.trim() ? [value.trim()] : [];
  if (Array.isArray(value)) return value.flatMap(flattenText);
  if (typeof value === "object") return Object.values(value as Record<string, unknown>).flatMap(flattenText);
  return [String(value)];
};

const linesFrom = (props: ProductionProps): string[] => {
  const explicit = (props.lines || []).filter(Boolean);
  const fromScript = flattenText(props.script).filter(Boolean);
  const lines = [...explicit, ...fromScript].slice(0, 8);
  return lines.length ? lines : [
    props.title || "Production draft",
    "Lead with the point.",
    "Show the evidence.",
    "End with the next action.",
  ];
};

const scenesFrom = (props: ProductionProps): Scene[] => {
  if (props.scenes && props.scenes.length) return props.scenes.slice(0, 6);
  const lines = linesFrom(props).slice(1, 5);
  return lines.map((line, index) => ({scene_id: `scene-${index + 1}`, title: `Scene ${index + 1}`, text: line}));
};

const captionFrom = (props: ProductionProps, frame: number): string => {
  const captions = (props.captions || []).filter(Boolean);
  if (!captions.length) return linesFrom(props)[Math.min(1, linesFrom(props).length - 1)] || "";
  const idx = Math.min(captions.length - 1, Math.floor(frame / 45));
  return captions[idx];
};

const Header = ({props, accent}: {props: ProductionProps; accent: string}) => (
  <div style={{display: "flex", justifyContent: "space-between", alignItems: "center"}}>
    <div style={{fontSize: 22, textTransform: "uppercase", color: accent, fontWeight: 800}}>
      {label[props.format || ""] || "Content Studio"}
    </div>
    <div style={{fontSize: 18, color: palette.pine, fontWeight: 700}}>WijerCo</div>
  </div>
);

const Shell = ({
  props,
  accent,
  children,
}: {
  props: ProductionProps;
  accent: string;
  children: React.ReactNode;
}) => {
  const frame = useCurrentFrame();
  const {durationInFrames} = useVideoConfig();
  const progress = interpolate(frame, [0, durationInFrames], [0, 100]);
  return (
    <AbsoluteFill style={{background: palette.paper, color: palette.ink, fontFamily: "Inter, Arial, sans-serif"}}>
      <div style={{position: "absolute", inset: 44, border: `2px solid ${palette.mist}`, padding: 42}}>
        <Header props={props} accent={accent} />
        {children}
      </div>
      <div style={{position: "absolute", left: 44, bottom: 30, height: 8, width: `${progress}%`, background: accent}} />
    </AbsoluteFill>
  );
};

const TitleSequence = ({props, accent}: {props: ProductionProps; accent: string}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const scale = spring({frame, fps, config: {damping: 18, stiffness: 90}});
  const lines = linesFrom(props);
  return (
    <Shell props={props} accent={accent}>
      <div style={{fontSize: 72, lineHeight: 0.95, maxWidth: 970, marginTop: 44, fontWeight: 850, transform: `scale(${scale})`, transformOrigin: "left top"}}>
        {props.title || lines[0]}
      </div>
      <div style={{display: "grid", gap: 18, marginTop: 54, maxWidth: 900}}>
        {lines.slice(1, 4).map((line, index) => (
          <div key={index} style={{fontSize: 32, lineHeight: 1.2, display: "flex", gap: 18}}>
            <span style={{color: accent, fontWeight: 850}}>0{index + 1}</span>
            <span>{line}</span>
          </div>
        ))}
      </div>
    </Shell>
  );
};

const SceneBoard = ({props, accent}: {props: ProductionProps; accent: string}) => {
  const scenes = scenesFrom(props);
  return (
    <Shell props={props} accent={accent}>
      <div style={{fontSize: 54, lineHeight: 1, marginTop: 36, fontWeight: 850}}>{props.title || "Scene plan"}</div>
      <div style={{display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 18, marginTop: 34}}>
        {scenes.slice(0, 4).map((scene, index) => (
          <div key={scene.scene_id || index} style={{background: "#fffaf0", border: `2px solid ${palette.mist}`, padding: 22, minHeight: 138}}>
            <div style={{fontSize: 20, color: accent, fontWeight: 850}}>{scene.title || `Scene ${index + 1}`}</div>
            <div style={{fontSize: 26, lineHeight: 1.18, marginTop: 12}}>{scene.text || scene.asset_id || "Reuse approved evidence asset."}</div>
          </div>
        ))}
      </div>
    </Shell>
  );
};

const CaptionFrame = ({props, accent}: {props: ProductionProps; accent: string}) => {
  const frame = useCurrentFrame();
  return (
    <Shell props={props} accent={accent}>
      <div style={{fontSize: 62, lineHeight: 1, marginTop: 42, fontWeight: 850}}>{props.title || "Talking-head clip"}</div>
      <div style={{position: "absolute", left: 42, right: 42, bottom: 76, background: "rgba(20,35,31,.92)", color: "white", padding: "22px 28px", fontSize: 30, lineHeight: 1.18}}>
        {captionFrom(props, frame)}
      </div>
    </Shell>
  );
};

export const LinkedInShort = (props: ProductionProps) => <TitleSequence accent={palette.gold} props={props} />;
export const ExplainerCarousel = (props: ProductionProps) => <SceneBoard accent={palette.pine} props={props} />;
export const TalkingHeadClip = (props: ProductionProps) => <CaptionFrame accent={palette.slate} props={props} />;
export const PolicyBriefing = (props: ProductionProps) => <TitleSequence accent="#8a5a16" props={props} />;
export const CourseTeaser = (props: ProductionProps) => <SceneBoard accent={palette.teal} props={props} />;
export const ProposalWalkthrough = (props: ProductionProps) => <SceneBoard accent="#6a5f35" props={props} />;
