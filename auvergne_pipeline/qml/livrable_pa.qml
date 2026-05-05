<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.34.0" styleCategories="Symbology|Labeling">
  <renderer-v2 attr="origine" type="categorizedSymbol" symbollevels="0" forceraster="0" enableorderby="0" referencescale="-1">
    <categories>
      <category symbol="0" value="existant" label="PA existant" render="true"/>
      <category symbol="1" value="cree" label="PA cree (orphelin)" render="true"/>
    </categories>
    <symbols>
      <symbol name="0" type="marker" clip_to_extent="1" force_rhr="0" alpha="1">
        <layer pass="0" class="SimpleMarker" locked="0" enabled="1">
          <prop k="color" v="0,100,255,255"/>
          <prop k="outline_color" v="0,0,0,255"/>
          <prop k="outline_width" v="0.3"/>
          <prop k="size" v="4"/>
          <prop k="name" v="circle"/>
        </layer>
      </symbol>
      <symbol name="1" type="marker" clip_to_extent="1" force_rhr="0" alpha="1">
        <layer pass="0" class="SimpleMarker" locked="0" enabled="1">
          <prop k="color" v="255,0,0,255"/>
          <prop k="outline_color" v="0,0,0,255"/>
          <prop k="outline_width" v="0.5"/>
          <prop k="size" v="5"/>
          <prop k="name" v="circle"/>
        </layer>
      </symbol>
    </symbols>
  </renderer-v2>
  <labeling type="simple">
    <settings>
      <text-style fontFamily="Arial" fontSize="8" fontWeight="50" textColor="0,0,0,255" fieldName="pa_id"/>
    </settings>
  </labeling>
</qgis>
