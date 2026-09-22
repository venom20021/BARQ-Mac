// ─── Cybernetic Humanoid GLSL Shaders ─────────────────────────────────
// Smooth holographic bust: larger particles that blend into continuous
// horizontal scanlines, not scattered dots.

export const scatteringVertexShader = /* glsl */ `
  attribute vec3 aTargetPosition;
  attribute vec3 aRandomOffset;
  attribute float aFlowSpeed;

  uniform float uTime;
  uniform float uProgress;
  uniform float uAudio;

  varying vec3 vPosition;
  varying float vDistanceToCore;

  vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}
  vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}
  vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}
  vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}

  float snoise(vec3 v){
    const vec2 C=vec2(1.0/6.0,1.0/3.0);
    const vec4 D=vec4(0.0,0.5,1.0,2.0);
    vec3 i=floor(v+dot(v,C.yyy));
    vec3 x0=v-i+dot(i,C.xxx);
    vec3 g=step(x0.yzx,x0.xyz);
    vec3 l=1.0-g;
    vec3 i1=min(g.xyz,l.zxy);
    vec3 i2=max(g.xyz,l.zxy);
    vec3 x1=x0-i1+C.xxx;
    vec3 x2=x0-i2+C.yyy;
    vec3 x3=x0-D.yyy;
    i=mod289(i);
    vec4 p=permute(permute(permute(
      i.z+vec4(0.0,i1.z,i2.z,1.0))
      +i.y+vec4(0.0,i1.y,i2.y,1.0))
      +i.x+vec4(0.0,i1.x,i2.x,1.0));
    float n_=0.142857142857;
    vec3 ns=n_*D.wyz-D.xzx;
    vec4 j=p-49.0*floor(p*ns.z*ns.z);
    vec4 x_=floor(j*ns.z);
    vec4 y_=floor(j-7.0*x_);
    vec4 x=x_*ns.x+ns.yyyy;
    vec4 y=y_*ns.x+ns.yyyy;
    vec4 h=1.0-abs(x)-abs(y);
    vec4 b0=vec4(x.xy,y.xy);
    vec4 b1=vec4(x.zw,y.zw);
    vec4 s0=floor(b0)*2.0+1.0;
    vec4 s1=floor(b1)*2.0+1.0;
    vec4 sh=-step(h,vec4(0.0));
    vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy;
    vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;
    vec3 p0=vec3(a0.xy,h.x);
    vec3 p1=vec3(a0.zw,h.y);
    vec3 p2=vec3(a1.xy,h.z);
    vec3 p3=vec3(a1.zw,h.w);
    vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
    p0*=norm.x;p1*=norm.y;p2*=norm.z;p3*=norm.w;
    vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0);
    m=m*m;
    return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
  }

  void main() {
    vec3 nexus = vec3(0.0, -0.5, 0.0);
    float turb = snoise(aTargetPosition*2.0+uTime*0.3)*0.06;
    vec3 nOff = vec3(
      snoise(aTargetPosition*1.5+uTime*0.2)*0.3,
      snoise(aTargetPosition*1.5+uTime*0.2+100.0)*0.15,
      snoise(aTargetPosition*1.5+uTime*0.2+200.0)*0.3
    );

    vec3 scattered = nexus + aRandomOffset*1.5 + nOff*(1.0-uProgress);
    vec3 assembled = aTargetPosition;
    float p = smoothstep(0.0,1.0,uProgress);
    vec3 pos = mix(scattered, assembled, p);

    // Subtle turbulence when assembled
    pos.x += snoise(vec3(pos.y*3.0, uTime*0.5, pos.z*3.0))*0.015*p;

    vPosition = pos;
    vDistanceToCore = length(pos - vec3(0.0,1.2,0.0));

    vec4 mv = modelViewMatrix * vec4(pos,1.0);
    // Crisp points: ~3.5px at z=4 so adjacent ring particles read as
    // continuous scanlines instead of overlapping soft blobs.
    gl_PointSize = 14.0 / -mv.z;
    gl_Position = projectionMatrix * mv;
  }
`;

export const scatteringFragmentShader = /* glsl */ `
  uniform float uTime;

  varying vec3 vPosition;
  varying float vDistanceToCore;

  void main() {
    vec2 uv = gl_PointCoord - vec2(0.5);
    float dist = length(uv);
    if (dist > 0.5) discard;

    // Hard-edged disc with 1px AA — keeps scanlines sharp
    float alpha = 1.0 - smoothstep(0.32, 0.5, dist);

    // Base color: cyan/electric blue
    vec3 col = vec3(0.0, 0.55, 0.8);

    // Horizontal scanline modulation: bright line centers, dark gaps
    // between rings (period matches ring spacing, not per-particle noise)
    float scan = 0.55 + 0.45 * sin(vPosition.y * 125.66); // 20 lines per unit
    col *= 0.6 + 0.5 * scan;
    alpha *= 0.45 + 0.55 * scan;

    // Traveling highlight band (classic hologram refresh sweep)
    float band = fract(vPosition.y * 0.5 - uTime * 0.25);
    float sweep = smoothstep(0.08, 0.0, abs(band - 0.5));
    col += vec3(0.0, 0.35, 0.5) * sweep * 0.6;

    // Amber core inside head
    if (vDistanceToCore < 0.7) {
      float g = pow(1.0 - vDistanceToCore/0.7, 2.5);
      vec3 amber = mix(vec3(0.85,0.45,0.0), vec3(1.0,0.75,0.15), sin(vPosition.y*15.0)*0.5+0.5);
      col = mix(col, amber, g*0.85);
      alpha = max(alpha, g*0.4);
    }

    // Rim glow at silhouette edges
    float rim = pow(dist*2.0, 3.0);
    col += vec3(0.0, 0.6, 0.9) * rim * 0.15;

    gl_FragColor = vec4(col, alpha * 0.85);
  }
`;
