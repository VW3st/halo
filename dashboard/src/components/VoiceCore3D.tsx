import { useMemo, useRef } from 'react'
import { Canvas, useFrame } from '@react-three/fiber'
import { EffectComposer, Bloom } from '@react-three/postprocessing'
import * as THREE from 'three'
import { audio } from '../lib/audioAnalyser'
import { useHaloStore } from '../store/voiceHarnessStore'

const NOISE = `
vec3 mod289v(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 mod289v(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 permute(vec4 x){return mod289v(((x*34.0)+1.0)*x);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}
float snoise(vec3 v){
  const vec2 C=vec2(1.0/6.0,1.0/3.0); const vec4 D=vec4(0.0,0.5,1.0,2.0);
  vec3 i=floor(v+dot(v,C.yyy)); vec3 x0=v-i+dot(i,C.xxx);
  vec3 g=step(x0.yzx,x0.xyz); vec3 l=1.0-g; vec3 i1=min(g.xyz,l.zxy); vec3 i2=max(g.xyz,l.zxy);
  vec3 x1=x0-i1+C.xxx; vec3 x2=x0-i2+C.yyy; vec3 x3=x0-D.yyy; i=mod289v(i);
  vec4 p=permute(permute(permute(i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));
  float n_=0.142857142857; vec3 ns=n_*D.wyz-D.xzx;
  vec4 j=p-49.0*floor(p*ns.z*ns.z); vec4 x_=floor(j*ns.z); vec4 y_=floor(j-7.0*x_);
  vec4 xs=x_*ns.x+ns.yyyy; vec4 ys=y_*ns.x+ns.yyyy; vec4 h=1.0-abs(xs)-abs(ys);
  vec4 b0=vec4(xs.xy,ys.xy); vec4 b1=vec4(xs.zw,ys.zw);
  vec4 s0=floor(b0)*2.0+1.0; vec4 s1=floor(b1)*2.0+1.0; vec4 sh=-step(h,vec4(0.0));
  vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy; vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;
  vec3 p0=vec3(a0.xy,h.x); vec3 p1=vec3(a0.zw,h.y); vec3 p2=vec3(a1.xy,h.z); vec3 p3=vec3(a1.zw,h.w);
  vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
  p0*=norm.x;p1*=norm.y;p2*=norm.z;p3*=norm.w;
  vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0); m=m*m;
  return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
}`

const C_IDLE_A = new THREE.Color(0x123a8c)
const C_IDLE_B = new THREE.Color(0x00e5ff)
const C_THINK_A = new THREE.Color(0x4c2d99)
const C_THINK_B = new THREE.Color(0xb794f4)
const C_SPEAK_A = new THREE.Color(0x995010)
const C_SPEAK_B = new THREE.Color(0xffaa3b)

function makeRng(seed: number) {
  let s = seed >>> 0
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0
    return s / 4294967296
  }
}

function Core() {
  const orbRef = useRef<THREE.Mesh>(null!)
  const pRef = useRef<THREE.Points>(null!)
  const r1 = useRef<THREE.Mesh>(null!)
  const r2 = useRef<THREE.Mesh>(null!)
  const lens = useRef<THREE.Mesh>(null!)
  const lineRef = useRef<THREE.Line>(null!)
  const audioEnv = useRef(0)
  const audioPeak = useRef(0)

  const orbMat = useMemo(
    () =>
      new THREE.ShaderMaterial({
        uniforms: {
          u_time: { value: 0 }, u_audio: { value: 0 }, u_speak: { value: 0 }, u_think: { value: 0 },
          u_a: { value: C_IDLE_A.clone() }, u_b: { value: C_IDLE_B.clone() },
        },
        transparent: true, blending: THREE.AdditiveBlending, depthWrite: false,
        vertexShader: `uniform float u_time;uniform float u_audio;uniform float u_speak;uniform float u_think;
          varying vec3 vN;varying vec3 vP;varying float vD;${NOISE}
          void main(){vec3 p=position;float n=snoise(p*1.55+vec3(u_time*0.22));float n2=snoise(p*3.1+vec3(u_time*0.38));
          float energy=smoothstep(0.0,1.0,u_audio);float d=(n*0.44+n2*0.18)*(0.11+energy*0.42+u_speak*0.16+u_think*0.12);p+=normal*d;
          p*=1.0+energy*0.025+u_speak*0.018;vD=d;
          vN=normalize(normalMatrix*normal);vec4 mv=modelViewMatrix*vec4(p,1.0);vP=mv.xyz;gl_Position=projectionMatrix*mv;}`,
        fragmentShader: `uniform float u_time;uniform float u_audio;uniform float u_speak;uniform float u_think;
          uniform vec3 u_a;uniform vec3 u_b;varying vec3 vN;varying vec3 vP;varying float vD;
          void main(){vec3 viewDir=normalize(-vP);float facing=max(dot(vN,viewDir),0.0);
          float fres=pow(1.0-facing,2.35);float inner=pow(facing,5.0)*(0.22+u_audio*0.35);
          float glint=pow(max(dot(normalize(vec3(-0.45,0.55,0.7)),vN),0.0),16.0)*(0.18+u_audio*0.2);
          vec3 col=mix(u_a,u_b,clamp(fres+u_speak*0.2+u_think*0.15,0.0,1.0));
          col+=vec3(0.65,0.95,1.0)*glint+vec3(0.2,0.7,1.0)*inner+vD*0.22;
          float pulse=0.86+0.035*sin(u_time*0.9)+0.018*sin(u_time*2.1);col*=pulse+u_audio*0.16;
          // edge-weighted alpha keeps this as a glassy volume instead of a flat neon ball
          gl_FragColor=vec4(col,0.07+fres*0.42+inner*0.18);}`,
      }),
    [],
  )

  const auraMat = useMemo(
    () =>
      new THREE.ShaderMaterial({
        uniforms: { u_audio: { value: 0 }, u_speak: { value: 0 }, u_think: { value: 0 } },
        transparent: true, side: THREE.BackSide, blending: THREE.AdditiveBlending, depthWrite: false,
        vertexShader: `varying vec3 vN;varying vec3 vP;void main(){vN=normalize(normalMatrix*normal);
          vec4 mv=modelViewMatrix*vec4(position,1.0);vP=mv.xyz;gl_Position=projectionMatrix*mv;}`,
        fragmentShader: `uniform float u_audio;uniform float u_speak;uniform float u_think;varying vec3 vN;varying vec3 vP;
          void main(){float f=pow(1.0-max(dot(vN,normalize(-vP)),0.0),4.0);
          vec3 base=mix(vec3(0.0,0.8,1.0),vec3(0.55,0.45,1.0),u_think);base=mix(base,vec3(1.0,0.6,0.3),u_speak);
          gl_FragColor=vec4(base,f*(0.26+u_audio*0.28+u_speak*0.18+u_think*0.16));}`,
      }),
    [],
  )

  // galaxy: spiral disk + spherical halo, per-particle nebula color
  const { pGeo, pMat } = useMemo(() => {
    const P = 1300
    const pos = new Float32Array(P * 3), siz = new Float32Array(P), pha = new Float32Array(P), col = new Float32Array(P * 3)
    const PAL = [[0.0, 0.9, 1.0], [0.29, 0.66, 1.0], [0.72, 0.58, 0.96], [0.9, 0.42, 0.85]]
    const rand = makeRng(0x48414c4f)
    for (let i = 0; i < P; i++) {
      let x, y, z
      if (i < P * 0.62) {
        const arm = Math.floor(rand() * 3)
        const rr = 0.8 + Math.pow(rand(), 0.7) * 1.55
        const ang = arm * (Math.PI * 2 / 3) + rr * 2.3 + (rand() - 0.5) * 0.55
        x = Math.cos(ang) * rr; z = Math.sin(ang) * rr; y = (rand() - 0.5) * 0.32 * (rr * 0.5)
      } else {
        const r = 0.9 + rand() * 1.4, phi = rand() * Math.PI * 2, co = rand() * 2 - 1, si = Math.sqrt(1 - co * co)
        x = r * si * Math.cos(phi); y = r * si * Math.sin(phi); z = r * co
      }
      pos[i * 3] = x; pos[i * 3 + 1] = y; pos[i * 3 + 2] = z
      siz[i] = 0.35 + rand() * 1.35; pha[i] = rand() * Math.PI * 2
      const rnd = rand()
      const c = PAL[rnd < 0.45 ? 0 : rnd < 0.72 ? 1 : rnd < 0.9 ? 2 : 3]
      col[i * 3] = c[0]; col[i * 3 + 1] = c[1]; col[i * 3 + 2] = c[2]
    }
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3))
    g.setAttribute('size', new THREE.BufferAttribute(siz, 1))
    g.setAttribute('phase', new THREE.BufferAttribute(pha, 1))
    g.setAttribute('acolor', new THREE.BufferAttribute(col, 3))
    const m = new THREE.ShaderMaterial({
      uniforms: { u_time: { value: 0 }, u_audio: { value: 0 }, u_speak: { value: 0 } },
      transparent: true, blending: THREE.AdditiveBlending, depthWrite: false,
      vertexShader: `uniform float u_time;uniform float u_audio;uniform float u_speak;
        attribute float size;attribute float phase;attribute vec3 acolor;varying float vA;varying vec3 vC;
        void main(){float pulse=sin(u_time*1.05+phase)*0.5+0.5;vA=(0.35+pulse*0.65)*(0.12+u_audio*0.24);
        vC=mix(acolor,vec3(1.0,0.6,0.28),u_speak*0.5);
        vec3 p=position;p.xz*=1.0+u_audio*0.045;
        vec4 mv=modelViewMatrix*vec4(p,1.0);gl_PointSize=size*(1.0+u_audio*1.15)*(220.0/-mv.z);gl_Position=projectionMatrix*mv;}`,
      fragmentShader: `varying float vA;varying vec3 vC;void main(){vec2 c=gl_PointCoord-0.5;float d=length(c);
        if(d>0.5)discard;float a=(1.0-d*2.0);a=a*a;gl_FragColor=vec4(vC,a*vA);}`,
    })
    return { pGeo: g, pMat: m }
  }, [])

  const ringMat = (color: number, opacity: number) =>
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity, blending: THREE.AdditiveBlending, depthWrite: false })
  // All rings sit on ONE shared tilt plane so they read as clean concentric
  // orbits instead of clashing hoops. Dim + thin so they frame the orb
  // rather than competing with it.
  const ringMats = useMemo(
    () => ({
      equator: ringMat(0x9fe8ff, 0.5), a: ringMat(0x66b6ff, 0.26), b: ringMat(0x8a7cf0, 0.16),
    }),
    [],
  )

  // inner waveform line geometry
  const lineGeo = useMemo(() => {
    const N = 160
    const arr = new Float32Array(N * 3)
    for (let i = 0; i < N; i++) { arr[i * 3] = (i / (N - 1) - 0.5) * 1.5; arr[i * 3 + 1] = 0; arr[i * 3 + 2] = 0.05 }
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.BufferAttribute(arr, 3))
    return g
  }, [])
  const lineMat = useMemo(
    () => new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending, depthWrite: false }),
    [],
  )

  const colA = useMemo(() => C_IDLE_A.clone(), [])
  const colB = useMemo(() => C_IDLE_B.clone(), [])

  useFrame((state) => {
    const t = state.clock.elapsedTime
    audio.sample()
    const st = useHaloStore.getState()
    const rawAudio = Math.min(audio.micLevel * 1.8, 1)
    audioPeak.current = Math.max(rawAudio, audioPeak.current * 0.94)
    audioEnv.current = THREE.MathUtils.lerp(audioEnv.current, audioPeak.current, rawAudio > audioEnv.current ? 0.32 : 0.075)
    const audioT = audioEnv.current
    const thinkT = st.thinking ? 1 : 0
    const speakT = st.speaking ? 1 : 0
    const ou = orbMat.uniforms, au = auraMat.uniforms, pu = pMat.uniforms
    ou.u_time.value = t
    ou.u_audio.value = THREE.MathUtils.lerp(ou.u_audio.value, audioT, 0.3)
    ou.u_think.value = THREE.MathUtils.lerp(ou.u_think.value, thinkT, 0.08)
    ou.u_speak.value = THREE.MathUtils.lerp(ou.u_speak.value, speakT, 0.12)
    au.u_audio.value = ou.u_audio.value; au.u_think.value = ou.u_think.value; au.u_speak.value = ou.u_speak.value
    pu.u_time.value = t; pu.u_audio.value = ou.u_audio.value; pu.u_speak.value = ou.u_speak.value

    const tgtA = speakT ? C_SPEAK_A : thinkT ? C_THINK_A : C_IDLE_A
    const tgtB = speakT ? C_SPEAK_B : thinkT ? C_THINK_B : C_IDLE_B
    colA.lerp(tgtA, 0.08); colB.lerp(tgtB, 0.08)
    ;(ou.u_a.value as THREE.Color).copy(colA)
    ;(ou.u_b.value as THREE.Color).copy(colB)

    if (orbRef.current) { orbRef.current.rotation.y = t * 0.16; orbRef.current.rotation.x = t * 0.1 }
    if (pRef.current) pRef.current.rotation.y = t * 0.05
    // gentle shared-plane precession around vertical — slow + seamless
    if (lens.current) lens.current.rotation.y = t * 0.06
    if (r1.current) r1.current.rotation.y = -t * 0.045
    if (r2.current) r2.current.rotation.y = t * 0.035

    // inner waveform from mic time-domain
    const arr = lineGeo.attributes.position.array as Float32Array
    const N = arr.length / 3
    if (audio.timeData) {
      const td = audio.timeData
      for (let i = 0; i < N; i++) arr[i * 3 + 1] = ((td[Math.floor((i / (N - 1)) * (td.length - 1))] - 128) / 128) * 0.5
    } else {
      for (let i = 0; i < N; i++) { const u = i / (N - 1); arr[i * 3 + 1] = 0.06 * Math.sin(u * Math.PI * 4 + t * 2.8) }
    }
    lineGeo.attributes.position.needsUpdate = true
  })

  return (
    <group>
      <points ref={pRef} geometry={pGeo} material={pMat} rotation={[0.42, 0, 0]} />
      <mesh ref={orbRef} material={orbMat}>
        {/* detail 4 (≈2.5k verts) not 6 (≈40k): the noise vertex shader runs
            per-vertex every frame; detail 6 spikes the GPU and, while Halo's
            whisper/kokoro/ollama also use it, starves into bad flicker. */}
        <icosahedronGeometry args={[0.85, 4]} />
      </mesh>
      <mesh material={auraMat}>
        <sphereGeometry args={[1.55, 64, 64]} />
      </mesh>
      {/* bright equator hugging the orb + two dimmer concentric orbits,
          all on the SAME tilt plane so they precess together cleanly */}
      <mesh ref={lens} material={ringMats.equator} rotation={[Math.PI / 2 - 0.34, 0, 0]}>
        <torusGeometry args={[1.0, 0.009, 8, 220]} />
      </mesh>
      <mesh ref={r1} material={ringMats.a} rotation={[Math.PI / 2 - 0.34, 0, 0]}>
        <torusGeometry args={[1.52, 0.0042, 8, 220]} />
      </mesh>
      <mesh ref={r2} material={ringMats.b} rotation={[Math.PI / 2 - 0.34, 0, 0]}>
        <torusGeometry args={[1.96, 0.0032, 8, 220]} />
      </mesh>
      {/* @ts-expect-error three Line element */}
      <line ref={lineRef} geometry={lineGeo} material={lineMat} />
    </group>
  )
}

export default function VoiceCore3D() {
  return (
    <Canvas
      camera={{ position: [0, 0, 5.3], fov: 45 }}
      gl={{ alpha: true, antialias: true, powerPreference: 'high-performance', stencil: false, depth: true }}
      dpr={[1, 1.5]}
      frameloop="always"
      style={{ position: 'absolute', inset: 0 }}
    >
      <Core />
      <EffectComposer multisampling={0}>
        {/* higher threshold => only the hottest pixels bloom, so the orb
            glows instead of flooding the whole panel with one colour */}
        <Bloom intensity={0.5} luminanceThreshold={0.5} luminanceSmoothing={0.75} radius={0.6} resolutionScale={0.5} />
      </EffectComposer>
    </Canvas>
  )
}
