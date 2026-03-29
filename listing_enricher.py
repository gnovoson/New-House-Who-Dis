#!/usr/bin/env python3
"""
Listing Enricher — Selenium-based detail scraper
=================================================
Takes the JSON output from redfin_fetcher.py (or any JSON with listing URLs)
and opens each listing in your real Chrome browser to scrape the detail fields
that the CSV doesn't include: heating type, central air, garage, year built, etc.

Requirements:
  pip install selenium

  That's it! Selenium 4.6+ auto-downloads the matching ChromeDriver for your
  installed Chrome — no manual driver setup needed.

Usage:
  python listing_enricher.py redfin_listings.json
  python listing_enricher.py redfin_listings.json --limit 10
  python listing_enricher.py redfin_listings.json --headed
  python listing_enricher.py --urls "https://www.redfin.com/MA/Milton/..."
  python listing_enricher.py redfin_listings.json -o enriched.json
  python listing_enricher.py redfin_listings.json --delay 4

Output:
  Writes an enriched JSON file ready to import into house-scorer.html.
  Fields filled: heatingType, centralAir, garageSpaces, garageAttached, yearBuilt.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except ImportError:
    print("ERROR: Selenium not installed. Run:")
    print("  pip install selenium")
    sys.exit(1)


# ===== SCRAPING LOGIC (JavaScript executed inside the browser page) =====

REDFIN_SCRAPE_JS = r"""
var data = {};
var allText = document.body.innerText;
var allTextLower = allText.toLowerCase();

// --- Price ---
var priceEl = document.querySelector('[data-rf-test-id="abp-price"] span, .statsValue [data-rf-test-id="abp-price"], .price-section .statsValue');
if (priceEl) data.price = parseInt(priceEl.textContent.replace(/[^0-9]/g, '')) || 0;

// --- Beds / Baths / Sqft ---
var statsEl = document.querySelector('.HomeMainStats, .home-main-stats-variant, .HomeInfoV2');
if (statsEl) {
    var st = statsEl.textContent;
    var bedsM = st.match(/(\d+)\s*[Bb]eds?/);
    var bathsM = st.match(/(\d+\.?\d*)\s*[Bb]aths?/);
    var sqftM = st.match(/([\d,]+)\s*[Ss]q\.?\s*[Ff]t/);
    if (bedsM) data.bedrooms = parseInt(bedsM[1]);
    if (bathsM) {
        var b = parseFloat(bathsM[1]);
        data.bathsFull = Math.floor(b);
        data.bathsHalf = b % 1 >= 0.4 ? 1 : 0;
    }
    if (sqftM) data.sqft = parseInt(sqftM[1].replace(/,/g, ''));
}

// --- Address ---
var addrEl = document.querySelector('[data-rf-test-id="abp-streetLine"] .street-address, .street-address, h1.homeAddress');
var cityEl = document.querySelector('[data-rf-test-id="abp-cityStateZip"], .dp-subtext .cityStateZip');
data.address = addrEl ? addrEl.textContent.trim() : '';
if (cityEl) data.address += (data.address ? ', ' : '') + cityEl.textContent.trim();
if (cityEl) data.town = cityEl.textContent.split(',')[0].trim();

// --- Year Built ---
var yearM = allText.match(/[Bb]uilt\s*(?:in\s*)?(\d{4})/);
if (yearM) data.yearBuilt = parseInt(yearM[1]);

// =============================================================
// AMENITY EXTRACTION
// Strategy: First collect ALL lines from the page, then do a
// multi-pass analysis. Redfin separates delivery method
// (Steam, Forced Air) from fuel (Gas, Oil) on different lines.
// =============================================================

// Collect all amenity elements
var amenityEls = document.querySelectorAll(
    '.amenity-group .amenity-attribute, ' +
    '.amenity-group .entryItem, ' +
    '.amenity-group li, ' +
    '.super-group .amenity-attribute, ' +
    '.super-group .entryItem, ' +
    '.super-group li, ' +
    '.PropertyDetailsContainer li, ' +
    '[class*="amenity"] li, ' +
    '[class*="amenity"] .entryItem, ' +
    '.keyDetail, ' +
    '.home-facts .fact-item, ' +
    '.listing-detail-item, ' +
    'tr'
);

// Collect all amenity text lines for multi-pass analysis
var allAmenityLines = [];
amenityEls.forEach(function(el) {
    allAmenityLines.push(el.textContent.trim().toLowerCase());
});

// Also split full page text into lines for broader matching
var pageLines = allTextLower.split('\n').map(function(l) { return l.trim(); }).filter(function(l) { return l.length > 0; });

// --- HEATING: Two-pass approach ---
// Pass 1: Look for explicit fuel lines (most reliable)
var fuelLine = '';
for (var i = 0; i < allAmenityLines.length; i++) {
    var line = allAmenityLines[i];
    if (line.includes('fuel') || line.includes('heat fuel') || line.includes('heating fuel')) {
        fuelLine = line;
        break;
    }
}
if (!fuelLine) {
    for (var i = 0; i < pageLines.length; i++) {
        if (pageLines[i].includes('fuel') && (pageLines[i].includes('heat') || pageLines[i].includes('gas') || pageLines[i].includes('oil') || pageLines[i].includes('electric'))) {
            fuelLine = pageLines[i];
            break;
        }
    }
}

// Classify fuel from fuel line
function classifyFuel(txt) {
    // Exclude utility provider lines like "electricity provided by energysage"
    var clean = txt.replace(/electric(ity)?\s+provided\s+by[^\n]*/g, '')
                   .replace(/est\.\s*\$[\d,]+\/month[^\n]*/g, '');
    if (clean.includes('natural gas') || (clean.includes('gas') && !clean.includes('no gas'))) return 'gas';
    if (clean.includes('oil') || clean.includes('fuel oil')) return 'oil';
    if (clean.includes('electric') && !clean.includes('electricity')) return 'electric';
    if (clean.includes('propane') || clean.includes('lp gas')) return 'gas';
    if (clean.includes('solar')) return 'electric';
    return '';
}

if (fuelLine) {
    data.heatingType = classifyFuel(fuelLine) || 'other';
}

// Pass 2: Look at heating lines (method + sometimes fuel combined)
if (!data.heatingType) {
    var allHeatText = '';
    for (var i = 0; i < allAmenityLines.length; i++) {
        var line = allAmenityLines[i];
        if (line.includes('heat') && !line.includes('heated floors') && !line.includes('heating & cooling')) {
            allHeatText += ' ' + line;
        }
    }
    // Also grab from page text
    for (var i = 0; i < pageLines.length; i++) {
        if (pageLines[i].match(/^heat(ing)?[\s:]/) || pageLines[i].match(/heat(ing)?\s*(type|system|source)/)) {
            allHeatText += ' ' + pageLines[i];
        }
    }

    if (allHeatText) {
        var fuel = classifyFuel(allHeatText);
        if (fuel) {
            data.heatingType = fuel;
        } else if (allHeatText.includes('heat pump') || allHeatText.includes('mini-split') || allHeatText.includes('mini split')) {
            data.heatingType = 'heatpump';
        } else if (allHeatText.includes('steam') || allHeatText.includes('baseboard') || allHeatText.includes('radiator') || allHeatText.includes('hot water') || allHeatText.includes('forced air') || allHeatText.includes('warm air')) {
            // Delivery method found but no fuel — check surrounding lines for fuel clues
            data._heatingMethod = allHeatText.trim();
            // Don't set 'other' yet — check full page for fuel nearby
        }
    }
}

// Pass 3: If we have a heating method but no fuel, scan the broader Heating & Cooling section
if (!data.heatingType && data._heatingMethod) {
    // Look for gas/oil/electric in ONLY the heating/cooling lines (stop before utility/location)
    var hcSectionText = '';
    var inHCSection = false;
    for (var i = 0; i < pageLines.length; i++) {
        if (pageLines[i].includes('heating & cooling') || pageLines[i].includes('heating and cooling')) inHCSection = true;
        else if (inHCSection && (
            pageLines[i].includes('parking') || pageLines[i].includes('interior') ||
            pageLines[i].includes('exterior') || pageLines[i].includes('property') ||
            pageLines[i].includes('electricity provided') || pageLines[i].includes('internet') ||
            pageLines[i].includes('location') || pageLines[i].includes('hoa ') ||
            pageLines[i].includes('school') || pageLines[i].includes('community') ||
            pageLines[i].includes('public facts') || pageLines[i].includes('est. $')
        )) inHCSection = false;
        if (inHCSection) hcSectionText += ' ' + pageLines[i];
    }
    if (hcSectionText) {
        var fuel = classifyFuel(hcSectionText);
        if (fuel) data.heatingType = fuel;
        else data.heatingType = 'other';
    } else {
        data.heatingType = 'other';
    }
}
delete data._heatingMethod;

// --- COOLING / AC ---
// Look in amenity lines first
for (var i = 0; i < allAmenityLines.length; i++) {
    var line = allAmenityLines[i];
    if (data.centralAir !== undefined && data.centralAir !== null) break;
    if (line.includes('cooling') || line.includes('air condition') || line.includes('a/c') || line.includes('central air')) {
        if (line.includes('central') || line.includes('a/c') || line.includes('air condition')) data.centralAir = true;
        else if (line.includes('none') || line === 'cooling' || line === 'cooling:') {
            // Just the header — check next lines
            for (var j = i+1; j < Math.min(i+4, allAmenityLines.length); j++) {
                var nextLine = allAmenityLines[j];
                if (nextLine.includes('central') || nextLine.includes('a/c')) { data.centralAir = true; break; }
                if (nextLine.includes('none') || nextLine.includes('no air') || nextLine.includes('window') || nextLine.includes('wall unit')) { data.centralAir = false; break; }
            }
        }
        else if (line.includes('window') || line.includes('wall')) data.centralAir = false;
        else if (line.includes('none')) data.centralAir = false;
    }
}

// Fallback: scan page text for cooling
if (data.centralAir === undefined || data.centralAir === null) {
    if (allTextLower.includes('central air') || allTextLower.includes('central a/c')) {
        data.centralAir = true;
    } else {
        // Scan the Heating & Cooling section in page text
        var inCoolSection = false;
        for (var i = 0; i < pageLines.length; i++) {
            if (pageLines[i].match(/^cooling/)) inCoolSection = true;
            else if (inCoolSection && pageLines[i].match(/^(heating|parking|interior|exterior|property|garage)/)) inCoolSection = false;
            if (inCoolSection) {
                if (pageLines[i].includes('central') || pageLines[i].includes('a/c')) { data.centralAir = true; break; }
                if (pageLines[i].includes('none') || pageLines[i].includes('no ')) { data.centralAir = false; break; }
            }
        }
    }
}

// --- GARAGE ---
for (var i = 0; i < allAmenityLines.length; i++) {
    var line = allAmenityLines[i];
    if (data.garageSpaces) break;
    if (line.includes('garage')) {
        var gm = line.match(/(\d+)\s*(?:car|space|stall)/);
        if (gm) data.garageSpaces = parseInt(gm[1]);
        else {
            gm = line.match(/garage\s*spaces?\s*:?\s*(\d+)/);
            if (gm) data.garageSpaces = parseInt(gm[1]);
            else data.garageSpaces = 1;
        }
        if (line.includes('attached')) data.garageAttached = true;
        if (line.includes('detached')) data.garageAttached = false;
    }
}

// Scan key-detail spans
document.querySelectorAll('.keyDetail span, .keyDetail-value, .keyDetail-content').forEach(function(el) {
    var txt = el.textContent.trim().toLowerCase();
    if (txt.includes('gas') && !data.heatingType) data.heatingType = 'gas';
    if (txt.includes('a/c') || txt.includes('central air')) data.centralAir = true;
    if (txt.includes('garage') && !data.garageSpaces) {
        var gm = txt.match(/(\d+)/);
        data.garageSpaces = gm ? parseInt(gm[1]) : 1;
    }
});

// Final garage fallback
if (!data.garageSpaces) {
    var garageM = allText.match(/[Gg]arage\s*[Ss]paces?\s*[:]*\s*(\d+)/);
    if (!garageM) garageM = allText.match(/(\d+)\s*[Cc]ar\s*[Gg]arage/);
    if (garageM) data.garageSpaces = parseInt(garageM[1]);
    else if (allTextLower.includes('garage')) data.garageSpaces = 1;
}
if (data.garageAttached === undefined) {
    data.garageAttached = allTextLower.includes('attached garage') || allTextLower.includes('att. garage');
}

// --- Lot Size ---
var lotM = allText.match(/([\d,.]+)\s*[Aa]cres?/);
if (lotM) data.lotAcres = parseFloat(lotM[1].replace(/,/g, ''));
else {
    var lotSqM = allText.match(/[Ll]ot\s*[Ss]ize[\s:]+\s*([\d,]+)\s*(?:sq|sf)/i);
    if (lotSqM) data.lotAcres = Math.round(parseInt(lotSqM[1].replace(/,/g, '')) / 43560 * 100) / 100;
}

// --- Open House ---
var ohEl = document.querySelector('.open-house-banner, .OpenHouseInfo, [data-rf-test-id="open-house-label"]');
if (ohEl) data.openHouseInfo = ohEl.textContent.trim();

// =============================================================
// KITCHEN & ENTERTAINING AUTO-SCORING
// Scan structured amenity data for quality signals
// =============================================================

// Collect all text from relevant sections into buckets
var kitchenText = '';
var interiorText = '';
var applianceText = '';
var livingText = '';
var diningText = '';
var exteriorText = '';
var bathroomText = '';
var basementText = '';

var curSect = '';
for (var si = 0; si < pageLines.length; si++) {
    var ln = pageLines[si];
    // Detect section headers
    if (ln.length < 50 && ln.length > 2) {
        if (ln.includes('kitchen')) curSect = 'kitchen';
        else if (ln.includes('appliance')) curSect = 'appliance';
        else if (ln.includes('interior feature')) curSect = 'interior';
        else if (ln.includes('interior')) curSect = 'interior';
        else if (ln.includes('living room') || ln.includes('family room') || ln.includes('great room')) curSect = 'living';
        else if (ln.includes('dining')) curSect = 'dining';
        else if (ln.includes('exterior feature') || ln.includes('patio') || ln.includes('deck')) curSect = 'exterior';
        else if (ln.includes('bathroom') || ln.includes('master bath')) curSect = 'bathroom';
        else if (ln.includes('basement')) curSect = 'basement';
        else if (ln.includes('parking') || ln.includes('school') || ln.includes('community') ||
                 ln.includes('tax') || ln.includes('financial') || ln.includes('listing') ||
                 ln.includes('sale history') || ln.includes('climate') || ln.includes('property type') ||
                 ln.includes('see less')) curSect = '';
    }
    if (curSect === 'kitchen') kitchenText += ' ' + ln;
    else if (curSect === 'appliance') applianceText += ' ' + ln;
    else if (curSect === 'interior') interiorText += ' ' + ln;
    else if (curSect === 'living') livingText += ' ' + ln;
    else if (curSect === 'dining') diningText += ' ' + ln;
    else if (curSect === 'exterior') exteriorText += ' ' + ln;
    else if (curSect === 'bathroom') bathroomText += ' ' + ln;
    else if (curSect === 'basement') basementText += ' ' + ln;
}

// --- KITCHEN SCORING (base 30, max ~95) ---
var kScore = 30; // bare minimum — has a kitchen
var kSignals = [];

// Countertops (big impact)
if (kitchenText.includes('granite') || kitchenText.includes('quartz') || kitchenText.includes('marble') || kitchenText.includes('stone/granite') || kitchenText.includes('solid surface')) { kScore += 15; kSignals.push('stone/granite counters'); }
else if (kitchenText.includes('countertop') || kitchenText.includes('counter')) { kScore += 5; kSignals.push('counters mentioned'); }

// Island
if (kitchenText.includes('island')) { kScore += 12; kSignals.push('kitchen island'); }

// Cabinets
if (kitchenText.includes('upgraded') || kitchenText.includes('custom') || kitchenText.includes('soft close')) { kScore += 8; kSignals.push('upgraded cabinets'); }

// Appliances
var allKitchenApp = kitchenText + ' ' + applianceText;
if (allKitchenApp.includes('stainless steel') || allKitchenApp.includes('stainless')) { kScore += 10; kSignals.push('stainless steel'); }
if (allKitchenApp.includes('double oven') || allKitchenApp.includes('wall oven')) { kScore += 5; kSignals.push('wall/double oven'); }
if (allKitchenApp.includes('wine') || allKitchenApp.includes('beverage')) { kScore += 3; kSignals.push('wine/beverage cooler'); }
if (allKitchenApp.includes('gas range') || allKitchenApp.includes('gas cook') || allKitchenApp.includes('gas stove')) { kScore += 4; kSignals.push('gas cooking'); }

// Open floor plan (big for kitchen feel)
if (kitchenText.includes('open floor') || kitchenText.includes('open concept') || kitchenText.includes('open floorplan') || interiorText.includes('open floor') || interiorText.includes('open concept')) { kScore += 10; kSignals.push('open floor plan'); }

// Pantry
if (kitchenText.includes('pantry') || kitchenText.includes('walk-in pantry')) { kScore += 4; kSignals.push('pantry'); }

// Flooring
if (kitchenText.includes('hardwood') || kitchenText.includes('engineered hardwood')) { kScore += 3; kSignals.push('hardwood floors'); }

// Recessed lighting
if (kitchenText.includes('recessed light')) { kScore += 2; kSignals.push('recessed lighting'); }

// Recently renovated kitchen
if (kitchenText.includes('renovated') || kitchenText.includes('remodeled') || kitchenText.includes('updated') || kitchenText.includes('new kitchen')) { kScore += 8; kSignals.push('renovated kitchen'); }

data.kitchenRating = Math.min(95, kScore);
data.kitchenSignals = kSignals;

// --- ENTERTAINING SCORING (base 25, max ~95) ---
var eScore = 25;
var eSignals = [];

// Open floor plan (biggest entertaining signal)
if (kitchenText.includes('open floor') || kitchenText.includes('open concept') || kitchenText.includes('open floorplan') || interiorText.includes('open floor') || interiorText.includes('open concept')) { eScore += 15; eSignals.push('open floor plan'); }

// Separate dining room
if (diningText.length > 20) { eScore += 8; eSignals.push('dining room'); }
if (diningText.includes('formal') || allTextLower.includes('formal dining')) { eScore += 4; eSignals.push('formal dining'); }

// Living/family/great room
if (livingText.length > 20) { eScore += 6; eSignals.push('living room'); }
if (allTextLower.includes('great room') || allTextLower.includes('family room')) { eScore += 6; eSignals.push('great/family room'); }
if (allTextLower.includes('bonus room') || allTextLower.includes('rec room') || allTextLower.includes('game room') || allTextLower.includes('media room')) { eScore += 5; eSignals.push('bonus/rec room'); }

// Fireplace
if (allTextLower.includes('fireplace')) { eScore += 6; eSignals.push('fireplace'); }

// Outdoor entertaining
if (exteriorText.includes('deck') || allTextLower.includes('deck')) { eScore += 6; eSignals.push('deck'); }
if (exteriorText.includes('patio') || allTextLower.includes('patio')) { eScore += 6; eSignals.push('patio'); }
if (exteriorText.includes('porch') || allTextLower.includes('screened porch') || allTextLower.includes('enclosed porch')) { eScore += 5; eSignals.push('porch'); }
if (allTextLower.includes('outdoor kitchen') || allTextLower.includes('built-in grill')) { eScore += 6; eSignals.push('outdoor kitchen'); }
if (allTextLower.includes('pool') || allTextLower.includes('in-ground pool')) { eScore += 6; eSignals.push('pool'); }
if (allTextLower.includes('hot tub') || allTextLower.includes('spa')) { eScore += 3; eSignals.push('hot tub/spa'); }

// Finished basement (extra entertaining space)
if (basementText.includes('finished') || allTextLower.includes('finished basement') || allTextLower.includes('walkout basement')) { eScore += 8; eSignals.push('finished basement'); }

// High ceilings / vaulted
if (allTextLower.includes('cathedral') || allTextLower.includes('vaulted') || allTextLower.includes('high ceiling') || allTextLower.includes('9 ft') || allTextLower.includes('10 ft')) { eScore += 4; eSignals.push('high ceilings'); }

// Wet bar
if (allTextLower.includes('wet bar') || allTextLower.includes('bar area')) { eScore += 5; eSignals.push('wet bar'); }

// Kitchen island (doubles as entertaining)
if (kitchenText.includes('island')) { eScore += 4; eSignals.push('kitchen island'); }

// Total room count boost (more rooms = more entertaining potential)
var roomMatch = interiorText.match(/# of rooms.*?(\d+)/);
if (roomMatch) {
    var totalRooms = parseInt(roomMatch[1]);
    if (totalRooms >= 10) { eScore += 6; eSignals.push(totalRooms + ' rooms'); }
    else if (totalRooms >= 8) { eScore += 4; eSignals.push(totalRooms + ' rooms'); }
    else if (totalRooms >= 6) { eScore += 2; eSignals.push(totalRooms + ' rooms'); }
}

data.entertainingRating = Math.min(95, eScore);
data.entertainingSignals = eSignals;

// --- RENOVATION RATING (bonus: auto-detect reno signals) ---
// Only set if we find strong signals; otherwise leave at default for manual
var rScore = 0;
var rSignals = [];
if (allTextLower.includes('renovated') || allTextLower.includes('remodeled') || allTextLower.includes('gut rehab')) { rScore += 30; rSignals.push('renovated'); }
if (allTextLower.includes('new kitchen') || kitchenText.includes('renovated') || kitchenText.includes('remodeled')) { rScore += 15; rSignals.push('kitchen renovated'); }
if (allTextLower.includes('new roof')) { rScore += 10; rSignals.push('new roof'); }
if (allTextLower.includes('new windows') || allTextLower.includes('replacement windows')) { rScore += 8; rSignals.push('new windows'); }
if (allTextLower.includes('new bath') || bathroomText.includes('renovated') || bathroomText.includes('remodeled')) { rScore += 10; rSignals.push('bath renovated'); }
if (allTextLower.includes('updated electric') || allTextLower.includes('new electric') || allTextLower.includes('200 amp')) { rScore += 5; rSignals.push('updated electric'); }
if (allTextLower.includes('new hvac') || allTextLower.includes('new furnace') || allTextLower.includes('new boiler')) { rScore += 5; rSignals.push('new HVAC'); }
if (allTextLower.includes('hardwood') && (allTextLower.includes('refinish') || allTextLower.includes('new floor'))) { rScore += 5; rSignals.push('refinished floors'); }
// For new construction, auto high reno
if (data.yearBuilt && data.yearBuilt >= 2015) { rScore = Math.max(rScore, 85); rSignals.push('new construction'); }
else if (data.yearBuilt && data.yearBuilt >= 2000) { rScore = Math.max(rScore, 60); rSignals.push('built after 2000'); }

if (rScore > 0) {
    data.renoRating = Math.min(95, Math.max(30, rScore));
    data.renoSignals = rSignals;
}

return data;
"""

ZILLOW_SCRAPE_JS = """
var data = {};
var allText = document.body.innerText;

// --- Price ---
var priceEl = document.querySelector('[data-testid="price"] span, .summary-container [data-testid="price"]');
if (priceEl) data.price = parseInt(priceEl.textContent.replace(/[^0-9]/g, '')) || 0;

// --- Beds / Baths / Sqft ---
var summaryEl = document.querySelector('.summary-container, [data-testid="bed-bath-beyond"]');
if (summaryEl) {
    var zt = summaryEl.textContent;
    var zBeds = zt.match(/(\\d+)\\s*bd/i);
    var zBaths = zt.match(/(\\d+\\.?\\d*)\\s*ba/i);
    var zSqft = zt.match(/([\\d,]+)\\s*sqft/i);
    if (zBeds) data.bedrooms = parseInt(zBeds[1]);
    if (zBaths) {
        var b = parseFloat(zBaths[1]);
        data.bathsFull = Math.floor(b);
        data.bathsHalf = b % 1 >= 0.4 ? 1 : 0;
    }
    if (zSqft) data.sqft = parseInt(zSqft[1].replace(/,/g, ''));
}

// --- Address ---
var addrEl = document.querySelector('[data-testid="bdp-hero-header"] h1, .summary-container h1');
data.address = addrEl ? addrEl.textContent.trim() : '';
var parts = data.address.split(',');
if (parts.length >= 2) data.town = parts[parts.length - 2].trim();

// --- Year Built ---
var yearM = allText.match(/[Bb]uilt\\s*(?:in\\s*)?(\\d{4})/);
if (yearM) data.yearBuilt = parseInt(yearM[1]);

// --- Heating ---
var heatM = allText.match(/[Hh]eat(?:ing)?\\s*(?:[Tt]ype)?[\\s:]+([^\\n,]+)/);
if (heatM) {
    var ht = heatM[1].toLowerCase();
    if (ht.includes('gas')) data.heatingType = 'gas';
    else if (ht.includes('oil')) data.heatingType = 'oil';
    else if (ht.includes('heat pump')) data.heatingType = 'heatpump';
    else if (ht.includes('electric')) data.heatingType = 'electric';
    else data.heatingType = 'other';
}

// --- Central Air ---
data.centralAir = allText.toLowerCase().includes('central air') || allText.toLowerCase().includes('central a/c');

// --- Garage ---
var garageM = allText.match(/(\\d+)\\s*[Cc]ar\\s*[Gg]arage/);
if (!garageM) garageM = allText.match(/[Gg]arage\\s*[Ss]paces?\\s*[:]*\\s*(\\d+)/);
data.garageSpaces = garageM ? parseInt(garageM[1]) : (allText.toLowerCase().includes('garage') ? 1 : 0);
data.garageAttached = allText.toLowerCase().includes('attached');

// --- Lot Size ---
var lotM = allText.match(/([\\d,.]+)\\s*[Aa]cres?/);
if (lotM) data.lotAcres = parseFloat(lotM[1].replace(/,/g, ''));

return data;
"""


# JavaScript to expand all collapsed amenity/detail sections on Redfin
EXPAND_SECTIONS_JS = r"""
var clicked = 0;

// Strategy 1: Click amenity group headers (the collapsible sections)
document.querySelectorAll('.amenity-group .title, .amenity-group-title, .super-group-title').forEach(function(el) {
    el.click(); clicked++;
});

// Strategy 2: Click any "See more" / "Show more" / expand buttons in the details area
document.querySelectorAll('button, a, span, div').forEach(function(el) {
    var txt = (el.textContent || '').trim().toLowerCase();
    if (txt === 'see more' || txt === 'show more' || txt === 'see all' ||
        txt === 'view more' || txt === 'more details' || txt === 'expand all' ||
        txt.match(/^show\s+\d+\s+more/) || txt === 'show all') {
        try { el.click(); clicked++; } catch(e) {}
    }
});

// Strategy 3: Click expand icons/carets in the property details section
document.querySelectorAll('.expandable-header, .collapsible-header, [class*="expand"], [class*="collapse"], [class*="toggle"]').forEach(function(el) {
    // Only click if it looks like it's in a details/amenity context
    var parent = el.closest('.amenity, .propertyDetails, .listing-details, .home-facts, .super-group, .amenity-group, .below-the-fold, [class*="amenit"], [class*="detail"], [class*="fact"]');
    if (parent) {
        try { el.click(); clicked++; } catch(e) {}
    }
});

// Strategy 4: Target Redfin's specific React component patterns
document.querySelectorAll('.entryItemContent, .amenityContainer .header, .PropertyDetailsContainer .expandableContent').forEach(function(el) {
    try { el.click(); clicked++; } catch(e) {}
});

// Strategy 5: Click any element with aria-expanded="false" in the details area
document.querySelectorAll('[aria-expanded="false"]').forEach(function(el) {
    var inDetails = el.closest('.below-the-fold, .propertyDetails, .super-group, [class*="amenity"], [class*="detail"], [class*="fact"], .PropertyHistory, .HomeInfo');
    if (inDetails) {
        try { el.click(); clicked++; } catch(e) {}
    }
});

return clicked;
"""


def detect_site(url):
    if 'redfin.com' in url:
        return 'redfin'
    elif 'zillow.com' in url:
        return 'zillow'
    return None


def create_driver(headed=False):
    """Create a Selenium Chrome driver using your installed Chrome."""
    opts = Options()
    if not headed:
        opts.add_argument('--headless=new')
    opts.add_argument('--window-size=1920,1080')
    opts.add_argument('--disable-blink-features=AutomationControlled')
    opts.add_argument('--disable-extensions')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
    # Suppress automation flags
    opts.add_experimental_option('excludeSwitches', ['enable-automation'])
    opts.add_experimental_option('useAutomationExtension', False)

    # Selenium 4.6+ auto-manages chromedriver via selenium-manager
    driver = webdriver.Chrome(options=opts)
    # Remove navigator.webdriver flag
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
        'source': 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
    })
    return driver


def enrich_listing(driver, listing, delay=2.0):
    """Navigate to a listing URL and scrape detail fields."""
    url = listing.get('url', '')
    if not url:
        return listing

    site = detect_site(url)
    if not site:
        print(f"  Skipping unsupported URL: {url[:80]}")
        return listing

    try:
        print(f"  Opening: {url[:90]}...")
        driver.get(url)

        # Wait for content to render
        wait = WebDriverWait(driver, 10)
        if site == 'redfin':
            try:
                wait.until(EC.presence_of_element_located((
                    By.CSS_SELECTOR,
                    '.HomeMainStats, .home-main-stats-variant, .HomeInfoV2, [data-rf-test-id="abp-price"]'
                )))
            except Exception:
                pass  # page may have loaded enough

            # Scroll down incrementally to trigger lazy-loaded content
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 3)")
            time.sleep(1)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 2 / 3)")
            time.sleep(1)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2)

            # Click ALL expandable amenity/detail sections to reveal hidden content
            expand_count = driver.execute_script(EXPAND_SECTIONS_JS)
            if expand_count:
                print(f"    (expanded {expand_count} sections)")
                time.sleep(1.5)

            scraped = driver.execute_script(REDFIN_SCRAPE_JS)

        elif site == 'zillow':
            try:
                wait.until(EC.presence_of_element_located((
                    By.CSS_SELECTOR,
                    '[data-testid="price"], .summary-container'
                )))
            except Exception:
                pass
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2)")
            time.sleep(1.5)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.5)
            scraped = driver.execute_script(ZILLOW_SCRAPE_JS)
        else:
            return listing

        # Merge scraped data — only overwrite empty/missing fields
        detail_fields = ['heatingType', 'centralAir', 'garageSpaces', 'garageAttached',
                         'yearBuilt', 'lotAcres', 'openHouseInfo']
        for key in detail_fields:
            scraped_val = scraped.get(key)
            existing_val = listing.get(key)
            if scraped_val not in (None, '', 0, False):
                if existing_val in (None, '', 0, False):
                    listing[key] = scraped_val

        # Kitchen, entertaining, reno — always overwrite since CSV defaults are 50
        for key in ['kitchenRating', 'entertainingRating', 'renoRating']:
            scraped_val = scraped.get(key)
            if scraped_val and scraped_val != 50:  # only if enricher found signals
                listing[key] = scraped_val

        # Store the signal lists as notes for transparency
        signals = []
        if scraped.get('kitchenSignals'):
            signals.append(f"Kitchen: {', '.join(scraped['kitchenSignals'])}")
        if scraped.get('entertainingSignals'):
            signals.append(f"Entertaining: {', '.join(scraped['entertainingSignals'])}")
        if scraped.get('renoSignals'):
            signals.append(f"Reno: {', '.join(scraped['renoSignals'])}")
        if signals:
            existing_notes = listing.get('notes', '')
            signal_text = ' | '.join(signals)
            if existing_notes and signal_text not in existing_notes:
                listing['notes'] = existing_notes + ' | ' + signal_text
            elif not existing_notes:
                listing['notes'] = signal_text

        # Fill in basics if CSV had zeros
        for key in ['price', 'bedrooms', 'bathsFull', 'bathsHalf', 'sqft']:
            if not listing.get(key) and scraped.get(key):
                listing[key] = scraped[key]

        if not listing.get('address') and scraped.get('address'):
            listing['address'] = scraped['address']
        if not listing.get('town') and scraped.get('town'):
            listing['town'] = scraped['town']

        listing['_source'] = f'{site}-enriched'
        listing['_enriched'] = datetime.now().isoformat()

        heat = listing.get('heatingType', '?')
        ac = 'AC' if listing.get('centralAir') else 'no-AC'
        garage = f"{listing.get('garageSpaces', 0)}G"
        year = listing.get('yearBuilt', '?')
        kit = listing.get('kitchenRating', 50)
        ent = listing.get('entertainingRating', 50)
        reno = listing.get('renoRating', 50)
        print(f"    > {listing.get('address', '?')[:45]} heat:{heat} {ac} {garage} yr:{year} kit:{kit} ent:{ent} reno:{reno}")
        if scraped.get('kitchenSignals'):
            print(f"      kitchen: {', '.join(scraped['kitchenSignals'])}")
        if scraped.get('entertainingSignals'):
            print(f"      entertaining: {', '.join(scraped['entertainingSignals'])}")
        if scraped.get('renoSignals'):
            print(f"      reno: {', '.join(scraped['renoSignals'])}")

        # Clean up debug/signal keys from listing
        for k in list(listing.keys()):
            if k.startswith('_debug') or k.endswith('Signals'):
                del listing[k]

        time.sleep(delay)
        return listing

    except Exception as e:
        print(f"    X Error scraping {url[:60]}: {e}")
        return listing


def run_enricher(listings, limit=None, headed=False, delay=2.0):
    """Launch Chrome and enrich all listings."""
    to_enrich = [l for l in listings if l.get('url')]
    if limit:
        to_enrich = to_enrich[:limit]

    print(f"\nEnriching {len(to_enrich)} listings using Selenium (Chrome)...")
    print(f"Mode: {'headed (visible browser)' if headed else 'headless'}")
    print(f"Delay: {delay}s between pages\n")

    driver = create_driver(headed=headed)

    try:
        # Warm up sessions
        redfin_urls = [l for l in to_enrich if 'redfin.com' in l.get('url', '')]
        zillow_urls = [l for l in to_enrich if 'zillow.com' in l.get('url', '')]

        if redfin_urls:
            print("Warming up Redfin session...")
            try:
                driver.get('https://www.redfin.com/')
                time.sleep(2)
                cookies = len(driver.get_cookies())
                print(f"  Got {cookies} cookies\n")
            except Exception as e:
                print(f"  Warning: Could not load Redfin homepage: {e}\n")

        if zillow_urls:
            print("Warming up Zillow session...")
            try:
                driver.get('https://www.zillow.com/')
                time.sleep(2)
                print(f"  Zillow cookies set\n")
            except Exception as e:
                print(f"  Warning: Could not load Zillow homepage: {e}\n")

        enriched_count = 0
        failed_count = 0
        consecutive_fails = 0

        for i, listing in enumerate(to_enrich, 1):
            if consecutive_fails >= 5:
                print(f"\n  5 consecutive failures — site may be blocking. Stopping.")
                break

            print(f"[{i}/{len(to_enrich)}]", end="")
            result = enrich_listing(driver, listing, delay=delay)

            if result.get('_enriched'):
                enriched_count += 1
                consecutive_fails = 0
            else:
                failed_count += 1
                consecutive_fails += 1

    finally:
        driver.quit()

    print(f"\n{'='*60}")
    print(f"Enrichment complete: {enriched_count} succeeded, {failed_count} failed")
    print(f"{'='*60}")

    return listings


def main():
    parser = argparse.ArgumentParser(
        description='Enrich house listings by scraping details with Selenium + Chrome',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Enrich listings from redfin_fetcher.py output
  python listing_enricher.py redfin_listings.json

  # Enrich only first 5 listings
  python listing_enricher.py redfin_listings.json --limit 5

  # Show the browser while it works
  python listing_enricher.py redfin_listings.json --headed

  # Enrich specific URLs directly
  python listing_enricher.py --urls "https://www.redfin.com/MA/Milton/123-Main/home/12345"

  # Custom output file
  python listing_enricher.py redfin_listings.json -o enriched_listings.json

  # Slower pace to avoid rate limiting
  python listing_enricher.py redfin_listings.json --delay 4
        """
    )
    parser.add_argument('input_file', nargs='?', help='JSON file from redfin_fetcher.py')
    parser.add_argument('--urls', nargs='+', help='Specific listing URLs to scrape')
    parser.add_argument('--limit', type=int, help='Max number of listings to enrich')
    parser.add_argument('--delay', type=float, default=2.0, help='Seconds between page loads (default: 2)')
    parser.add_argument('--headed', action='store_true', help='Show the browser window')
    parser.add_argument('-o', '--output', help='Output file (default: adds "_enriched" to input filename)')

    args = parser.parse_args()

    if not args.input_file and not args.urls:
        parser.print_help()
        print("\nError: Provide an input JSON file or --urls")
        sys.exit(1)

    # Load listings
    listings = []
    if args.input_file:
        if not os.path.exists(args.input_file):
            print(f"Error: File not found: {args.input_file}")
            sys.exit(1)
        with open(args.input_file) as f:
            listings = json.load(f)
        print(f"Loaded {len(listings)} listings from {args.input_file}")

    # Add URL-only listings
    if args.urls:
        for url in args.urls:
            if not any(l.get('url') == url for l in listings):
                listings.append({
                    'url': url,
                    'address': '',
                    'town': '',
                    'price': 0,
                    'bedrooms': 0,
                    'bathsFull': 0,
                    'bathsHalf': 0,
                    'sqft': 0,
                    'yearBuilt': 0,
                    'heatingType': '',
                    'centralAir': None,
                    'garageSpaces': 0,
                    'garageAttached': False,
                    'kitchenRating': 50,
                    'entertainingRating': 50,
                    'renoRating': 50,
                    'streetRating': 50,
                    'offices': 0,
                    'notes': '',
                    '_source': 'url-input',
                    '_fetched': datetime.now().isoformat(),
                    'id': f"h-{int(datetime.now().timestamp())}-{len(listings)}",
                })

    # Filter to those needing enrichment
    needs_enrichment = []
    already_enriched = []
    for l in listings:
        if l.get('_enriched'):
            already_enriched.append(l)
        elif not l.get('heatingType') or l.get('centralAir') is None or not l.get('garageSpaces'):
            needs_enrichment.append(l)
        else:
            already_enriched.append(l)

    if already_enriched:
        print(f"  {len(already_enriched)} listings already have detail data (skipping)")
    print(f"  {len(needs_enrichment)} listings need enrichment")

    if not needs_enrichment:
        print("Nothing to enrich!")
        sys.exit(0)

    # Run enrichment
    run_enricher(needs_enrichment, limit=args.limit, headed=args.headed, delay=args.delay)

    # Determine output file
    if args.output:
        out_path = args.output
    elif args.input_file:
        base, ext = os.path.splitext(args.input_file)
        out_path = f"{base}_enriched{ext}"
    else:
        out_path = "enriched_listings.json"

    # Assign IDs
    for i, l in enumerate(listings):
        if not l.get('id'):
            l['id'] = f"h-{int(datetime.now().timestamp())}-{i}"

    # Write output
    with open(out_path, 'w') as f:
        json.dump(listings, f, indent=2)

    print(f"\nSaved {len(listings)} listings to: {out_path}")

    # Summary
    enriched = [l for l in listings if l.get('_enriched')]
    if enriched:
        heat_counts = {}
        ac_count = 0
        garage_count = 0
        for l in enriched:
            ht = l.get('heatingType', 'unknown')
            heat_counts[ht] = heat_counts.get(ht, 0) + 1
            if l.get('centralAir'):
                ac_count += 1
            if l.get('garageSpaces', 0) > 0:
                garage_count += 1

        print(f"\nEnrichment summary ({len(enriched)} listings):")
        print(f"  Heating: {', '.join(f'{k}={v}' for k, v in sorted(heat_counts.items()))}")
        print(f"  Central Air: {ac_count}/{len(enriched)}")
        print(f"  Has Garage: {garage_count}/{len(enriched)}")

    print(f"\nNext: Open house-scorer.html > Import JSON > select {out_path}")


if __name__ == '__main__':
    main()
