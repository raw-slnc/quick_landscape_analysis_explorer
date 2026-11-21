(function () {
  'use strict';

  const DATE_MIN = 2024.866;
  const DATE_MAX = 2030.0;
  const ELLIPSOID = {
    a: 6378.137,
    b: 6356.7523142,
    epssq: 0.0066943799901413165,
    re: 6371.2
  };

  let loadedModel = null;
  let modelPromise = null;

  function decimalYearFromDate(date) {
    const year = date.getUTCFullYear();
    const start = Date.UTC(year, 0, 1);
    const end = Date.UTC(year + 1, 0, 1);
    return year + (date.getTime() - start) / (end - start);
  }

  function decimalYearFromSlashDate(text) {
    const match = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(text.trim());
    if (!match) return null;
    const month = Number.parseInt(match[1], 10);
    const day = Number.parseInt(match[2], 10);
    const year = Number.parseInt(match[3], 10);
    const start = Date.UTC(year, 0, 1);
    const current = Date.UTC(year, month - 1, day);
    const end = Date.UTC(year + 1, 0, 1);
    return year + (current - start) / (end - start);
  }

  function parseModel(text) {
    const lines = text.replace(/\r/g, '').split('\n');
    const header = (lines.shift() || '').trim().split(/\s+/);
    if (header.length < 3) throw new Error('WMMHR header parse failed');

    const epoch = Number.parseFloat(header[0]);
    const minYear = decimalYearFromSlashDate(header[2]) ?? epoch;
    const records = [];
    let nMax = 0;
    let nMaxSecVar = 0;

    for (const rawLine of lines) {
      const line = rawLine.trim();
      if (!line || line.startsWith('9999')) break;
      const parts = line.split(/\s+/);
      if (parts.length < 6) continue;

      const n = Number.parseInt(parts[0], 10);
      const m = Number.parseInt(parts[1], 10);
      const g = Number.parseFloat(parts[2]);
      const h = Number.parseFloat(parts[3]);
      const dg = Number.parseFloat(parts[4]);
      const dh = Number.parseFloat(parts[5]);
      records.push({ n, m, g, h, dg, dh });
      if (n > nMax) nMax = n;
      if (Math.abs(dg) > 0 || Math.abs(dh) > 0) nMaxSecVar = Math.max(nMaxSecVar, n);
    }

    const numTerms = ((nMax + 1) * (nMax + 2)) / 2;
    const mainFieldCoeffG = new Float64Array(numTerms + 1);
    const mainFieldCoeffH = new Float64Array(numTerms + 1);
    const secularVarCoeffG = new Float64Array(numTerms + 1);
    const secularVarCoeffH = new Float64Array(numTerms + 1);

    for (const record of records) {
      const index = (record.n * (record.n + 1)) / 2 + record.m;
      mainFieldCoeffG[index] = record.g;
      mainFieldCoeffH[index] = record.h;
      secularVarCoeffG[index] = record.dg;
      secularVarCoeffH[index] = record.dh;
    }

    return {
      epoch,
      minYear,
      maxYear: epoch + 5,
      nMax,
      nMaxSecVar,
      mainFieldCoeffG,
      mainFieldCoeffH,
      secularVarCoeffG,
      secularVarCoeffH
    };
  }

  async function loadModel(modelPath) {
    if (loadedModel) return loadedModel;
    if (modelPromise) return modelPromise;

    modelPromise = fetch(modelPath, { cache: 'force-cache' })
      .then((response) => {
        if (!response.ok) throw new Error(`WMMHR fetch failed: ${response.status}`);
        return response.text();
      })
      .then((text) => {
        loadedModel = parseModel(text);
        return loadedModel;
      })
      .catch((error) => {
        modelPromise = null;
        throw error;
      });

    return modelPromise;
  }

  function geodeticToSpherical(lat, lng, heightKm) {
    const safeLat = Math.max(-89.99999, Math.min(89.99999, lat));
    const latRad = safeLat * Math.PI / 180;
    const cosLat = Math.cos(latRad);
    const sinLat = Math.sin(latRad);
    const rc = ELLIPSOID.a / Math.sqrt(1 - ELLIPSOID.epssq * sinLat * sinLat);
    const xp = (rc + heightKm) * cosLat;
    const zp = (rc * (1 - ELLIPSOID.epssq) + heightKm) * sinLat;
    const r = Math.sqrt(xp * xp + zp * zp);
    return {
      lambda: lng,
      phig: Math.asin(zp / r) * 180 / Math.PI,
      r
    };
  }

  function computeSphericalHarmonicVariables(coordSpherical, nMax) {
    const relativeRadiusPower = new Float64Array(nMax + 1);
    const cosMLambda = new Float64Array(nMax + 1);
    const sinMLambda = new Float64Array(nMax + 1);
    const lambdaRad = coordSpherical.lambda * Math.PI / 180;
    const cosLambda = Math.cos(lambdaRad);
    const sinLambda = Math.sin(lambdaRad);
    const radiusRatio = ELLIPSOID.re / coordSpherical.r;

    relativeRadiusPower[0] = radiusRatio * radiusRatio;
    for (let n = 1; n <= nMax; n++) {
      relativeRadiusPower[n] = relativeRadiusPower[n - 1] * radiusRatio;
    }

    cosMLambda[0] = 1;
    sinMLambda[0] = 0;
    if (nMax >= 1) {
      cosMLambda[1] = cosLambda;
      sinMLambda[1] = sinLambda;
    }
    for (let m = 2; m <= nMax; m++) {
      cosMLambda[m] = cosMLambda[m - 1] * cosLambda - sinMLambda[m - 1] * sinLambda;
      sinMLambda[m] = cosMLambda[m - 1] * sinLambda + sinMLambda[m - 1] * cosLambda;
    }

    return { relativeRadiusPower, cosMLambda, sinMLambda };
  }

  function computePcupHigh(x, nMax) {
    const numTerms = ((nMax + 1) * (nMax + 2)) / 2;
    const z = Math.sqrt((1 - x) * (1 + x));
    if (z === 0 || Math.abs(x) === 1) return null;

    const Pcup = new Float64Array(numTerms + 1);
    const dPcup = new Float64Array(numTerms + 1);
    const f1 = new Float64Array(numTerms + 1);
    const f2 = new Float64Array(numTerms + 1);
    const preSqr = new Float64Array(2 * nMax + 2);
    const scalef = 1e-280;

    for (let n = 0; n <= 2 * nMax + 1; n++) preSqr[n] = Math.sqrt(n);

    let k = 2;
    for (let n = 2; n <= nMax; n++) {
      k += 1;
      f1[k] = (2 * n - 1) / n;
      f2[k] = (n - 1) / n;
      for (let m = 1; m <= n - 2; m++) {
        k += 1;
        f1[k] = (2 * n - 1) / preSqr[n + m] / preSqr[n - m];
        f2[k] = preSqr[n - m - 1] * preSqr[n + m - 1] / preSqr[n + m] / preSqr[n - m];
      }
      k += 2;
    }

    let pm2 = 1;
    Pcup[0] = 1;
    dPcup[0] = 0;
    if (nMax === 0) return { Pcup, dPcup };

    let pm1 = x;
    Pcup[1] = pm1;
    dPcup[1] = z;
    k = 1;

    for (let n = 2; n <= nMax; n++) {
      k += n;
      const plm = f1[k] * x * pm1 - f2[k] * pm2;
      Pcup[k] = plm;
      dPcup[k] = n * (pm1 - x * plm) / z;
      pm2 = pm1;
      pm1 = plm;
    }

    let pmm = preSqr[2] * scalef;
    let rescalem = 1 / scalef;
    let kstart = 0;
    let m = 1;

    for (m = 1; m <= nMax - 1; ++m) {
      rescalem *= z;
      kstart += m + 1;
      pmm = pmm * preSqr[2 * m + 1] / preSqr[2 * m];
      Pcup[kstart] = pmm * rescalem / preSqr[2 * m + 1];
      dPcup[kstart] = -(m * x * Pcup[kstart] / z);
      pm2 = pmm / preSqr[2 * m + 1];

      k = kstart + m + 1;
      pm1 = x * preSqr[2 * m + 1] * pm2;
      Pcup[k] = pm1 * rescalem;
      dPcup[k] = ((pm2 * rescalem) * preSqr[2 * m + 1] - x * (m + 1) * Pcup[k]) / z;

      for (let n = m + 2; n <= nMax; ++n) {
        k += n;
        const plm = x * f1[k] * pm1 - f2[k] * pm2;
        Pcup[k] = plm * rescalem;
        dPcup[k] = (preSqr[n + m] * preSqr[n - m] * (pm1 * rescalem) - n * x * Pcup[k]) / z;
        pm2 = pm1;
        pm1 = plm;
      }
    }

    rescalem *= z;
    kstart += m + 1;
    pmm = pmm / preSqr[2 * nMax];
    Pcup[kstart] = pmm * rescalem;
    dPcup[kstart] = -(nMax * x * Pcup[kstart] / z);
    return { Pcup, dPcup };
  }

  function computePcupLow(x, nMax) {
    const numTerms = ((nMax + 1) * (nMax + 2)) / 2;
    const Pcup = new Float64Array(numTerms + 1);
    const dPcup = new Float64Array(numTerms + 1);
    const schmidtQuasiNorm = new Float64Array(numTerms + 1);
    const z = Math.sqrt((1 - x) * (1 + x));

    Pcup[0] = 1;
    dPcup[0] = 0;

    for (let n = 1; n <= nMax; n++) {
      for (let m = 0; m <= n; m++) {
        const index = (n * (n + 1)) / 2 + m;
        if (n === m) {
          const index1 = ((n - 1) * n) / 2 + m - 1;
          Pcup[index] = z * Pcup[index1];
          dPcup[index] = z * dPcup[index1] + x * Pcup[index1];
        } else if (n === 1 && m === 0) {
          const index1 = ((n - 1) * n) / 2 + m;
          Pcup[index] = x * Pcup[index1];
          dPcup[index] = x * dPcup[index1] - z * Pcup[index1];
        } else {
          const index1 = ((n - 2) * (n - 1)) / 2 + m;
          const index2 = ((n - 1) * n) / 2 + m;
          if (m > n - 2) {
            Pcup[index] = x * Pcup[index2];
            dPcup[index] = x * dPcup[index2] - z * Pcup[index2];
          } else {
            const k = (((n - 1) * (n - 1)) - (m * m)) / ((2 * n - 1) * (2 * n - 3));
            Pcup[index] = x * Pcup[index2] - k * Pcup[index1];
            dPcup[index] = x * dPcup[index2] - z * Pcup[index2] - k * dPcup[index1];
          }
        }
      }
    }

    schmidtQuasiNorm[0] = 1;
    for (let n = 1; n <= nMax; n++) {
      let index = (n * (n + 1)) / 2;
      let index1 = ((n - 1) * n) / 2;
      schmidtQuasiNorm[index] = schmidtQuasiNorm[index1] * (2 * n - 1) / n;
      for (let m = 1; m <= n; m++) {
        index = (n * (n + 1)) / 2 + m;
        index1 = (n * (n + 1)) / 2 + m - 1;
        schmidtQuasiNorm[index] = schmidtQuasiNorm[index1] * Math.sqrt(((n - m + 1) * (m === 1 ? 2 : 1)) / (n + m));
      }
    }

    for (let n = 1; n <= nMax; n++) {
      for (let m = 0; m <= n; m++) {
        const index = (n * (n + 1)) / 2 + m;
        Pcup[index] *= schmidtQuasiNorm[index];
        dPcup[index] *= -schmidtQuasiNorm[index];
      }
    }

    return { Pcup, dPcup };
  }

  function computeAssociatedLegendre(coordSpherical, nMax) {
    const sinPhi = Math.sin(coordSpherical.phig * Math.PI / 180);
    if (nMax <= 16 || (1 - Math.abs(sinPhi)) < 1e-10) {
      return computePcupLow(sinPhi, nMax);
    }
    return computePcupHigh(sinPhi, nMax);
  }

  function sumField(model, coordSpherical, harmonicVariables, legendre, decimalYear) {
    const dt = decimalYear - model.epoch;
    const secVarLimit = (model.nMaxSecVar * (model.nMaxSecVar + 1)) / 2 + model.nMaxSecVar;
    const results = { Bx: 0, By: 0, Bz: 0 };

    for (let n = 1; n <= model.nMax; n++) {
      for (let m = 0; m <= n; m++) {
        const index = (n * (n + 1)) / 2 + m;
        const g = model.mainFieldCoeffG[index] + (index <= secVarLimit ? dt * model.secularVarCoeffG[index] : 0);
        const h = model.mainFieldCoeffH[index] + (index <= secVarLimit ? dt * model.secularVarCoeffH[index] : 0);
        const cosTerm = harmonicVariables.cosMLambda[m];
        const sinTerm = harmonicVariables.sinMLambda[m];
        const commonTerm = g * cosTerm + h * sinTerm;

        results.Bz -= harmonicVariables.relativeRadiusPower[n] * commonTerm * (n + 1) * legendre.Pcup[index];
        results.By += harmonicVariables.relativeRadiusPower[n] * (g * sinTerm - h * cosTerm) * m * legendre.Pcup[index];
        results.Bx -= harmonicVariables.relativeRadiusPower[n] * commonTerm * legendre.dPcup[index];
      }
    }

    const cosPhi = Math.cos(coordSpherical.phig * Math.PI / 180);
    if (Math.abs(cosPhi) > 1e-10) {
      results.By /= cosPhi;
      return results;
    }

    let by = 0;
    const sinPhi = Math.sin(coordSpherical.phig * Math.PI / 180);
    const pcupS = new Float64Array(model.nMax + 1);
    pcupS[0] = 1;
    let schmidtQuasiNorm1 = 1;

    for (let n = 1; n <= model.nMax; n++) {
      const index = (n * (n + 1)) / 2 + 1;
      const schmidtQuasiNorm2 = schmidtQuasiNorm1 * (2 * n - 1) / n;
      const schmidtQuasiNorm3 = schmidtQuasiNorm2 * Math.sqrt((n * 2) / (n + 1));
      schmidtQuasiNorm1 = schmidtQuasiNorm2;

      if (n === 1) {
        pcupS[n] = pcupS[n - 1];
      } else {
        const k = (((n - 1) * (n - 1)) - 1) / ((2 * n - 1) * (2 * n - 3));
        pcupS[n] = sinPhi * pcupS[n - 1] - k * pcupS[n - 2];
      }

      const g = model.mainFieldCoeffG[index] + (index <= secVarLimit ? dt * model.secularVarCoeffG[index] : 0);
      const h = model.mainFieldCoeffH[index] + (index <= secVarLimit ? dt * model.secularVarCoeffH[index] : 0);
      by += harmonicVariables.relativeRadiusPower[n]
        * (g * harmonicVariables.sinMLambda[1] - h * harmonicVariables.cosMLambda[1])
        * pcupS[n]
        * schmidtQuasiNorm3;
    }

    results.By = by;
    return results;
  }

  function rotateMagneticVector(coordSpherical, lat, resultsSpherical) {
    const psi = (coordSpherical.phig - lat) * Math.PI / 180;
    return {
      Bx: resultsSpherical.Bx * Math.cos(psi) - resultsSpherical.Bz * Math.sin(psi),
      By: resultsSpherical.By,
      Bz: resultsSpherical.Bx * Math.sin(psi) + resultsSpherical.Bz * Math.cos(psi)
    };
  }

  async function calculateDeclination(options) {
    const modelPath = options.modelPath || 'WMMHR.COF';
    const model = await loadModel(modelPath);
    const date = options.date || new Date();
    const decimalYear = decimalYearFromDate(date);
    if (decimalYear < Math.max(model.minYear, DATE_MIN) || decimalYear > Math.min(model.maxYear, DATE_MAX)) {
      return null;
    }

    const lat = options.lat;
    const lng = options.lng;
    const heightKm = options.heightKm || 0;
    const coordSpherical = geodeticToSpherical(lat, lng, heightKm);
    const harmonicVariables = computeSphericalHarmonicVariables(coordSpherical, model.nMax);
    const legendre = computeAssociatedLegendre(coordSpherical, model.nMax);
    if (!legendre) return null;

    const resultsSpherical = sumField(model, coordSpherical, harmonicVariables, legendre, decimalYear);
    const resultsGeodetic = rotateMagneticVector(coordSpherical, lat, resultsSpherical);
    return Math.atan2(resultsGeodetic.By, resultsGeodetic.Bx) * 180 / Math.PI;
  }

  window.FieldmapWmmhr = {
    preloadModel: loadModel,
    calculateDeclination,
    decimalYearFromDate
  };
})();
