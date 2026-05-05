<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.34.0" styleCategories="Symbology|Labeling">
  <renderer-v2 attr="cls" type="categorizedSymbol" symbollevels="0" forceraster="0" enableorderby="0" referencescale="-1">
    <categories>
      <category symbol="0" value="AUTO_OK" label="AUTO_OK (D3 ok)" render="true"/>
      <category symbol="1" value="TO_CREATE" label="TO_CREATE (D3 > 100m)" render="true"/>
    </categories>
    <symbols>
      <symbol name="0" type="marker" clip_to_extent="1" force_rhr="0" alpha="1">
        <layer pass="0" class="SimpleMarker" locked="0" enabled="1">
          <prop k="color" v="0,200,0,255"/>
          <prop k="outline_color" v="0,0,0,255"/>
          <prop k="outline_width" v="0.2"/>
          <prop k="size" v="3"/>
          <prop k="name" v="square"/>
        </layer>
      </symbol>
      <symbol name="1" type="marker" clip_to_extent="1" force_rhr="0" alpha="1">
        <layer pass="0" class="SimpleMarker" locked="0" enabled="1">
          <prop k="color" v="255,165,0,255"/>
          <prop k="outline_color" v="0,0,0,255"/>
          <prop k="outline_width" v="0.2"/>
          <prop k="size" v="3"/>
          <prop k="name" v="square"/>
        </layer>
      </symbol>
    </symbols>
  </renderer-v2>
  <labeling type="simple">
    <settings>
      <text-style fontFamily="Arial" fontSize="7" fontWeight="50" textColor="50,50,50,255" fieldName="d3_m" format="%1$.1f"/>
    </settings>
  </labeling>
</qgis>
