// Diagnostic only: does not modify the Telegram bot.
// Run with Node.js 20+: npm install youtubei.js && node youtubejs_probe.mjs
import { Innertube } from 'youtubei.js';

const videoId = 'YefI9Vz7ET0';
try {
  const youtube = await Innertube.create();
  const info = await youtube.getBasicInfo(videoId);
  const details = info.basic_info || {};
  const playable = info.streaming_data?.formats?.length || 0;
  console.log(JSON.stringify({
    success: true,
    id: videoId,
    title: details.title || null,
    playability: info.playability_status?.status || null,
    muxed_formats: playable,
    adaptive_formats: info.streaming_data?.adaptive_formats?.length || 0
  }));
} catch (error) {
  console.error(JSON.stringify({
    success: false,
    error_type: error?.name || 'Error',
    message: String(error?.message || error).slice(0, 300)
  }));
  process.exitCode = 1;
}
